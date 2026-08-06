import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()   # loads .env for local dev — no-op if it doesn't exist (Railway injects env vars directly)

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import json
import shutil
import tempfile

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Tuple
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from engine.runner import run_pricing, run_ifrs17, run_rate_table, run_reserving, run_nic_summary
from engine.ifrs17_nonlife import generate_nonlife_paa_statements
from engine.data_loader import load_paid_claims, load_rbc_solvency_data
from engine.rbc.data_model import (
    CreditRiskExposures, InsuranceRiskExposures, LegacySolvencyInputs, MarketRiskExposures,
    OperationalRiskExposures, QualifyingCapitalResources,
)
from engine.rbc.insurance_risk import calculate_insurance_risk
from engine.rbc.market_risk import calculate_market_risk
from engine.rbc.credit_risk import calculate_credit_risk
from engine.rbc.operational_risk import calculate_operational_risk
from engine.rbc.aggregation import calculate_solvency, SolvencyResult
from engine.rbc.legacy_solvency import calculate_legacy_solvency
from engine.rbc.stress_tests import run_stress_tests
from outputs.solvency_word_exporter import (
    generate_girbc_certificate_from_results, GENERATED_DIR as SOLVENCY_WORD_GENERATED_DIR,
)
from engine.journals import generate_nonlife_journal
from engine.clients import list_clients, load_client
from engine.assumptions_manager import AssumptionSet
from engine.assumptions import ProductAssumptions, LapseSchedule
from engine.custom_pricing import (
    build_custom_product, run_custom_pricing, run_custom_rate_table, run_custom_sensitivity,
    SENSITIVITY_STRESSES, SENSITIVITY_STRESSES_SUMMARY,
)
from engine.investment_analysis import project_investment_fund, summarise_investment_fund
from outputs.custom_pricing_excel_exporter import export_custom_pricing_to_excel, GENERATED_DIR as CUSTOM_PRICING_EXCEL_DIR
from outputs.custom_pricing_word_exporter import generate_custom_pricing_avr_note, GENERATED_DIR as CUSTOM_PRICING_WORD_DIR
from outputs.custom_pricing_memo_exporter import generate_actuarial_memorandum, GENERATED_DIR as CUSTOM_PRICING_MEMO_DIR
from outputs.excel_exporter import export_nonlife_statements_to_excel, GENERATED_DIR

from db.database import DATABASE_URL, SessionLocal, engine as db_engine, get_db, init_db
from db.models import User, ValuationRun, UserRole, AssumptionSet as AssumptionSetModel, ReservingNote, SolvencyRun, RunSignOff
from auth.dependencies import get_current_user, get_current_admin
from auth.router import router as auth_router

@asynccontextmanager
async def _lifespan(app: FastAPI):
    # init_db() (db/database.py) already catches and logs its own errors —
    # it never raises. This try/except is a deliberate second layer: NOTHING
    # in startup should ever be allowed to stop uvicorn from binding and
    # serving /health. Before this existed, an unreachable database made
    # create_all() raise here, uvicorn logged "Application startup failed.
    # Exiting.", and the process exited without binding to a port at all —
    # every route 502'd, not just the ones that touch the database.
    print("STARTUP: lifespan begin")
    try:
        print("STARTUP: step 1/1 — database init starting...")
        init_db()
        print("STARTUP: step 1/1 — database init finished.")
    except Exception as e:
        print(f"STARTUP WARNING: unexpected error escaped init_db() — {type(e).__name__}: {e}")
        print("STARTUP WARNING: continuing to start the app anyway.")
    print("STARTUP: lifespan complete — uvicorn will now accept requests")
    yield
    print("SHUTDOWN: lifespan ending")


app = FastAPI(
    title="AMVS — Ghana Actuarial Modelling & Valuation System",
    description="Actuarial pricing, IFRS 17 valuation, and NIC reporting.",
    version="1.0.0",
    lifespan=_lifespan,
)

# Comma-separated list of allowed origins in production (see .env.example).
# https://nasiruchristiantalamah-crypto.github.io — the GitHub Pages-hosted
# dashboard/nic_report (see docs/) — is always allowed regardless of what
# ALLOWED_ORIGINS is set to on Railway. It's this project's own known
# frontend; forgetting to include it in that env var would silently break
# it with a CORS error that's easy to misdiagnose as "the API is down"
# (the dashboard's health check just shows offline, no CORS-specific
# message — browsers don't surface CORS failures as a distinguishable
# fetch() error).
GITHUB_PAGES_ORIGIN = "https://nasiruchristiantalamah-crypto.github.io"

_env_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _env_origins:
    _allowed_origins = sorted(set(_env_origins) | {GITHUB_PAGES_ORIGIN})
else:
    # No ALLOWED_ORIGINS set at all (e.g. local dev without a .env) — allow
    # everything, same permissive default as before.
    _allowed_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _log_valuation_run(
    db: Session, user: User, run_type: str, inputs: dict, outputs: Optional[dict] = None,
    client_id: Optional[str] = None,
) -> None:
    """
    Best-effort audit log of a valuation run — a logging failure must never
    break the actual valuation response, so this swallows and prints rather
    than raising.
    """
    try:
        db.add(ValuationRun(
            user_id=user.id, client_id=client_id, run_type=run_type,
            inputs=inputs, outputs=outputs,
        ))
        db.commit()
    except Exception as e:
        print(f"WARNING: failed to log valuation run '{run_type}' for user {user.email}: {e}")
        db.rollback()


# Every route below (except "/", "/health", and "/auth/*") requires a valid
# bearer token — see auth/dependencies.py's get_current_user.
protected = APIRouter()

app.include_router(auth_router)

# NOTE on client_id/product_name: the engine (engine/runner.py) is now
# fully multi-client — see clients/<client_id>/. These request models keep
# the OLD tier/product shape for backward compatibility with the current
# dashboard UI (a client/product selector is Phase 6); _resolve_product_name()
# below maps tier/product onto a clients/pic/products/*.yaml product name.
# `dependant` is currently accepted but not applied — dependants are now a
# per-product structural choice (Product.dependants), not a per-request
# override; overriding them at request time is a Phase 6 UI feature.

def _resolve_product_name(product: str, tier: int) -> str:
    if product == "micro_life":
        return "micro_life"
    return f"whole_life_tier{tier}"

DEFAULT_CLIENT = "pic"

# Railway can't host clients' real Excel workbooks (private, large,
# machine-specific paths — see engine/clients.py's <CLIENT_ID>_DATA_DIR
# handling). Non-life routes call this before touching engine/data_loader.py
# so a missing data folder fails as a clear 503, not a confusing
# FileNotFoundError several layers down. Life endpoints (pricing, IFRS 17
# GMM) never call this — they don't read from a data folder at all.
DATA_UNAVAILABLE_DETAIL = "Data files not available on this server — contact Stallion Consultants to arrange data access"

def _require_data_access(client_id: str) -> None:
    try:
        client = load_client(client_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not client.data_dir_available:
        raise HTTPException(status_code=503, detail=DATA_UNAVAILABLE_DETAIL)


class PricingRequest(BaseModel):
    entry_age:         int             = Field(35,   ge=18, le=70)
    tier:              int             = Field(1,    ge=1,  le=3)
    product:           str             = Field("whole_life")
    dependant:         str             = Field("spouse")
    mortality_loading: Optional[float] = Field(None, description="Quick override for just mortality loading — superseded by `assumptions` if both are given")
    target_margin:     Optional[float] = Field(None, ge=0, le=0.5, description="Quick override for just the target profit margin — superseded by `assumptions` if both are given")
    assumptions:       Optional[dict]  = Field(None, description="Full engine.assumptions_manager.AssumptionSet.to_dict() shape — see GET /assumptions/defaults. Overrides the product's saved basis for this run only; omit to use the product's own saved assumptions.")

class IFRS17Request(BaseModel):
    company_name:    str            = Field(...)
    product_type:    str            = Field("whole_life")
    period:          str            = Field("FY2025")
    period_index:    int            = Field(1, ge=1)
    in_force_count:  int            = Field(1000, ge=1)
    entry_age:       int            = Field(35,   ge=18, le=70)
    tier:            int            = Field(1,    ge=1,  le=3)
    reporting_freq:  str            = Field("annual")
    assumptions:     Optional[dict] = Field(None, description="Full AssumptionSet override — see PricingRequest.assumptions")

class RateTableRequest(BaseModel):
    tier:        int             = Field(1,  ge=1,  le=3)
    product:     str             = Field("whole_life")
    age_start:   int             = Field(18, ge=18, le=70)
    age_end:     int             = Field(70, ge=18, le=70)
    assumptions: Optional[dict]  = Field(None, description="Full AssumptionSet override — see PricingRequest.assumptions")

class ReservingRequest(BaseModel):
    class_of_business:          str                     = Field(..., description="e.g. Motor, Fire, Accident, Others")
    gross_triangle:             Dict[int, List[float]]  = Field(..., description="origin_year -> cumulative incurred by development age")
    net_triangle:                Dict[int, List[float]] = Field(..., description="same shape, net of reinsurance")
    method:                       str                    = Field("chain_ladder", description="chain_ladder, bornhuetter_ferguson, or cape_cod")
    gross_premium:                Optional[Dict[int, float]] = Field(None, description="required if method=bornhuetter_ferguson or cape_cod")
    net_premium:                   Optional[Dict[int, float]] = Field(None, description="required if method=bornhuetter_ferguson or cape_cod")
    expected_loss_ratio_gross:      Optional[float]      = Field(None, ge=0, le=5)
    expected_loss_ratio_net:         Optional[float]      = Field(None, ge=0, le=5)

@app.get("/")
def welcome(request: Request):
    return {
        "system":  "AMVS — Ghana Actuarial Modelling & Valuation System",
        "version": "1.0.0",
        "status":  "running",
        "company": "Stallion Consultants Ltd",
        "docs":    str(request.url_for("swagger_ui_html")),
    }

@app.get("/health")
def health_check():
    return {"status": "healthy", "engine": "operational"}

def _mask_database_url(url: str) -> str:
    """postgresql://user:secret@host:5432/dbname -> postgresql://user:****@host:5432/dbname"""
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
        if parts.password:
            netloc = netloc.replace(f":{parts.password}@", ":****@")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return "(could not parse DATABASE_URL)"


@app.get("/db/status")
def db_status():
    """
    Unauthenticated diagnostic — deliberately not behind auth, since its
    whole purpose is diagnosing why auth itself might be broken (e.g.
    POST /auth/login 500ing because the users table doesn't exist or the
    database is unreachable). The connection string is shown with its
    password masked — safe to leave public, and far more useful for
    catching a typo'd host/port/dbname than a bare scheme would be.

    This answers the same question db/database.py's init_db() startup log
    line does, but on demand — useful when Railway's deployment logs
    aren't quick to reach, or when the failure happens well after startup
    (e.g. a database that was reachable at boot but isn't anymore).
    """
    result = {
        "database_url_configured": bool(os.environ.get("DATABASE_URL")),
        "database_url_masked":     _mask_database_url(DATABASE_URL),
        "secret_key_configured":   bool(os.environ.get("SECRET_KEY")),
        "database_reachable":      False,
        "users_table_exists":      False,
        "user_count":              None,
        "error":                   None,
    }
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            result["database_reachable"] = True

            table_names = inspect(db_engine).get_table_names()
            result["users_table_exists"] = "users" in table_names
            if result["users_table_exists"]:
                result["user_count"] = db.query(User).count()
            else:
                result["error"] = (
                    "users table does not exist — init_db() likely failed at startup; "
                    "check the deployment logs for a line starting 'STARTUP WARNING: init_db() failed'"
                )
        finally:
            db.close()
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result

@protected.get("/clients")
def list_clients_endpoint():
    return {
        "success": True,
        "data": [
            {"client_id": cid, "name": load_client(cid).name, "currency": load_client(cid).currency}
            for cid in list_clients()
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
#  ADMINISTRATION — read-only list views, admin-only
# ══════════════════════════════════════════════════════════════════════════════
# Client Management and Assumption Sets reuse GET /clients and GET
# /assumptions/list respectively (already built) — these two are the only
# genuinely new endpoints the Administration nav group needs. Both are
# view-only: no create/edit/delete yet, by design (see the dashboard's
# Administration pages for the explicit "view only" notice).

@protected.get("/admin/users")
def admin_list_users(db: Session = Depends(get_db), current_admin: User = Depends(get_current_admin)):
    users = db.query(User).order_by(User.created_at.desc()).all()
    return {
        "success": True,
        "data": [
            {
                "id": u.id, "email": u.email, "company_name": u.company_name,
                "role": u.role.value if hasattr(u.role, "value") else u.role,
                "client_id": u.client_id, "is_active": u.is_active,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ],
    }


@protected.get("/admin/audit-trail")
def admin_list_audit_trail(
    limit: int = 200, db: Session = Depends(get_db), current_admin: User = Depends(get_current_admin),
):
    runs = (
        db.query(ValuationRun)
        .order_by(ValuationRun.created_at.desc())
        .limit(min(limit, 500))
        .all()
    )
    user_emails = {u.id: u.email for u in db.query(User).all()}

    run_ids = [r.id for r in runs]
    signoffs_by_run: Dict[int, List[dict]] = {rid: [] for rid in run_ids}
    if run_ids:
        signoffs = (
            db.query(RunSignOff)
            .filter(RunSignOff.valuation_run_id.in_(run_ids))
            .order_by(RunSignOff.created_at.asc())
            .all()
        )
        for s in signoffs:
            signoffs_by_run[s.valuation_run_id].append({
                "id": s.id, "reviewer_email": user_emails.get(s.reviewer_id, f"user #{s.reviewer_id}"),
                "note": s.note, "created_at": s.created_at.isoformat() if s.created_at else None,
            })

    return {
        "success": True,
        "data": [
            {
                "id": r.id, "user_email": user_emails.get(r.user_id, f"user #{r.user_id}"),
                "client_id": r.client_id, "run_type": r.run_type,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "signoffs": signoffs_by_run.get(r.id, []),
            }
            for r in runs
        ],
    }


class RunSignOffRequest(BaseModel):
    note: Optional[str] = Field(None, max_length=2000)

@protected.post("/admin/audit-trail/{run_id}/signoff")
def admin_signoff_run(
    run_id: int, request: RunSignOffRequest,
    db: Session = Depends(get_db), current_admin: User = Depends(get_current_admin),
):
    run = db.query(ValuationRun).filter(ValuationRun.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail=f"Valuation run #{run_id} not found")

    signoff = RunSignOff(valuation_run_id=run_id, reviewer_id=current_admin.id, note=request.note)
    db.add(signoff)
    db.commit()
    db.refresh(signoff)

    return {
        "success": True,
        "data": {
            "id": signoff.id, "reviewer_email": current_admin.email,
            "note": signoff.note, "created_at": signoff.created_at.isoformat(),
        },
    }


@protected.delete("/admin/audit-trail/signoff/{signoff_id}")
def admin_delete_signoff(
    signoff_id: int, db: Session = Depends(get_db), current_admin: User = Depends(get_current_admin),
):
    """
    Retract a sign-off — e.g. it was recorded against the wrong run, or the
    reviewer wants to withdraw it pending further review. Any admin can
    retract any sign-off (this system doesn't distinguish which admin
    reviews what), same access level as recording one.
    """
    signoff = db.query(RunSignOff).filter(RunSignOff.id == signoff_id).first()
    if signoff is None:
        raise HTTPException(status_code=404, detail=f"Sign-off #{signoff_id} not found")
    db.delete(signoff)
    db.commit()
    return {"success": True, "data": {"deleted_id": signoff_id}}


# ══════════════════════════════════════════════════════════════════════════════
#  ASSUMPTIONS MANAGER — user-editable, named, saveable assumption sets
# ══════════════════════════════════════════════════════════════════════════════
# See engine/assumptions_manager.py's AssumptionSet — this is the request-time
# override layer (dashboard Assumptions page), distinct from a client/
# product's own saved engine.assumptions.ProductAssumptions (clients/<id>/
# assumptions/). Every pricing/IFRS17/rate-table request above can carry an
# `assumptions` field built from one of these saved sets.

class AssumptionSetSaveRequest(BaseModel):
    name:         str            = Field(..., description="Display name, e.g. 'PIC Pricing Basis 2025'")
    description:  str            = Field("")
    client_id:    Optional[str]  = Field(None, description="Which insurer this basis is for, if any")
    assumptions:  dict           = Field(..., description="Full AssumptionSet.to_dict() shape — see GET /assumptions/defaults")
    is_default:   bool           = Field(False)


def _assumption_set_row_to_dict(row: AssumptionSetModel) -> dict:
    import json
    return {
        "id":          row.id,
        "name":        row.name,
        "description": row.description,
        "client_id":   row.client_id,
        "user_id":     row.user_id,
        "is_default":  row.is_default,
        "created_at":  row.created_at.isoformat() if row.created_at else None,
        "updated_at":  row.updated_at.isoformat() if row.updated_at else None,
        "assumptions": json.loads(row.assumptions_json),
    }


@protected.get("/assumptions/defaults")
def get_assumptions_defaults():
    return {"success": True, "data": AssumptionSet.ghana_defaults().to_dict()}


@protected.get("/assumptions/client/{client_id}")
def get_assumptions_for_client(client_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = (
        db.query(AssumptionSetModel)
        .filter(AssumptionSetModel.client_id == client_id)
        .order_by(AssumptionSetModel.created_at.desc())
        .all()
    )
    return {"success": True, "data": [_assumption_set_row_to_dict(r) for r in rows]}


@protected.post("/assumptions/save")
def save_assumption_set(request: AssumptionSetSaveRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    import json
    try:
        # Validate the payload actually parses into a real AssumptionSet
        # (catches typos/malformed nested groups) before persisting it —
        # save the RE-SERIALISED, validated form, not the raw request body.
        parsed = AssumptionSet.from_dict(request.assumptions)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid assumptions payload: {e}")

    row = AssumptionSetModel(
        name             = request.name,
        description      = request.description,
        client_id        = request.client_id,
        user_id          = current_user.id,
        is_default       = request.is_default,
        assumptions_json = json.dumps(parsed.to_dict()),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"success": True, "data": _assumption_set_row_to_dict(row)}


@protected.get("/assumptions/list")
def list_assumption_sets(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = (
        db.query(AssumptionSetModel)
        .filter(AssumptionSetModel.user_id == current_user.id)
        .order_by(AssumptionSetModel.created_at.desc())
        .all()
    )
    return {"success": True, "data": [_assumption_set_row_to_dict(r) for r in rows]}


@protected.delete("/assumptions/{assumption_set_id}")
def delete_assumption_set(assumption_set_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(AssumptionSetModel).filter(AssumptionSetModel.id == assumption_set_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Assumption set not found")
    if row.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not authorized to delete this assumption set")
    db.delete(row)
    db.commit()
    return {"success": True, "data": {"deleted_id": assumption_set_id}}


@protected.post("/pricing")
def pricing_endpoint(request: PricingRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        assumption_set = AssumptionSet.from_dict(request.assumptions) if request.assumptions else None
        result = run_pricing(
            client_id          = DEFAULT_CLIENT,
            product_name       = _resolve_product_name(request.product, request.tier),
            entry_age          = request.entry_age,
            mortality_loading  = request.mortality_loading,
            target_margin      = request.target_margin,
            assumption_set     = assumption_set,
            verbose            = False,
        )
        # inputs carries both the raw request AND the full assumptions basis
        # actually applied (result["assumptions_used"]) — the audit trail
        # this run can always be reproduced from, whether or not the caller
        # supplied a custom `assumptions` override.
        audit_inputs = {**request.model_dump(), "assumptions_used": result["assumptions_used"]}
        _log_valuation_run(db, current_user, "pricing", audit_inputs, result, client_id=DEFAULT_CLIENT)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _build_ifrs17_response_data(report: dict) -> dict:
    """
    Flatten a run_ifrs17() report into the JSON-friendly shape both
    POST /ifrs17 and POST /export/excel return/export — one shared builder
    so the numbers in the API response and the Excel workbook can never
    drift apart from each other.
    """
    nic = report["nic_report"]
    isr = report["insurance_service_result"]
    pv  = report["pv_summary"]
    csm = report["csm_rollforward"]
    return {
        "period":            report["period"],
        "company":           report["company"],
        "product":           report["product"],
        "measurement_model": report["measurement_model"],
        "in_force_count":    report["in_force_count"],
        "currency":          report["currency"],
        "lrc": {
            "pvfcf":           round(nic.lrc_gmm_pvfcf, 2),
            "risk_adjustment": round(nic.lrc_gmm_ra, 2),
            "csm":             round(nic.lrc_gmm_csm, 2),
            "total":           round(nic.lrc_gmm_pvfcf + nic.lrc_gmm_ra + nic.lrc_gmm_csm, 2),
        },
        "lic": {
            "best_estimate":   round(nic.lic_best_estimate, 2),
            "risk_adjustment": round(nic.lic_ra, 2),
            "total":           round(nic.lic_best_estimate + nic.lic_ra, 2),
        },
        "total_liabilities":     round(nic.total_liabilities, 2),
        "total_liabilities_usd": round(nic.total_liabilities_usd, 2),
        "pnl": {
            "insurance_revenue":        round(isr.total_insurance_revenue, 2),
            "insurance_expenses":       round(isr.total_insurance_expenses, 2),
            "insurance_service_result": round(isr.insurance_service_result, 2),
            "csm_amortisation":         round(isr.csm_amortisation, 2),
            "ra_release":               round(isr.ra_release, 2),
        },
        "csm_rollforward": {
            "opening":            round(csm.opening_csm, 2),
            "interest_accretion": round(csm.interest_accretion, 2),
            "amortisation":       round(csm.csm_amortisation, 2),
            "closing":            round(csm.closing_csm, 2),
        },
        "solvency": {
            "capital_adequacy_ratio": round(nic.capital_adequacy_ratio, 4),
            "is_solvent":             nic.is_solvent,
            "available_capital":      round(nic.available_capital, 2),
            "required_capital":       round(nic.solvency_capital_req, 2),
        },
        "pricing": {
            "monthly_premium": round(pv.pv_premiums / report["in_force_count"] / 12, 2),
            "pv_premiums":     round(pv.pv_premiums, 2),
            "pv_benefits":     round(pv.pv_benefits, 2),
            "profit_margin":   round(pv.profit_margin, 4),
            "is_onerous":      pv.is_onerous,
        },
        "assumptions_used": report["assumptions_used"],
    }


def _run_ifrs17_from_request(request: "IFRS17Request") -> dict:
    assumption_set = AssumptionSet.from_dict(request.assumptions) if getattr(request, "assumptions", None) else None
    report = run_ifrs17(
        client_id       = DEFAULT_CLIENT,
        product_name    = _resolve_product_name(request.product_type, request.tier),
        company_name    = request.company_name,
        period          = request.period,
        period_index    = request.period_index,
        in_force_count  = request.in_force_count,
        entry_age       = request.entry_age,
        reporting_freq  = request.reporting_freq,
        assumption_set  = assumption_set,
        verbose         = False,
    )
    return _build_ifrs17_response_data(report)


@protected.post("/ifrs17")
def ifrs17_endpoint(request: IFRS17Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        response_data = _run_ifrs17_from_request(request)
        audit_inputs = {**request.model_dump(), "assumptions_used": response_data["assumptions_used"]}
        _log_valuation_run(db, current_user, "ifrs17", audit_inputs, response_data, client_id=DEFAULT_CLIENT)
        return {"success": True, "data": response_data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@protected.post("/rate-table")
def rate_table_endpoint(request: RateTableRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        assumption_set = AssumptionSet.from_dict(request.assumptions) if request.assumptions else None
        result = run_rate_table(
            client_id       = DEFAULT_CLIENT,
            product_name    = _resolve_product_name(request.product, request.tier),
            age_start       = request.age_start,
            age_end         = request.age_end,
            assumption_set  = assumption_set,
            verbose         = False,
        )
        audit_inputs = {**request.model_dump(), "assumptions_used": result["assumptions_used"]}
        _log_valuation_run(db, current_user, "rate_table", audit_inputs, result, client_id=DEFAULT_CLIENT)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
#  CUSTOM PRODUCT PRICING (Part 4) — the dashboard's product builder
# ══════════════════════════════════════════════════════════════════════════════
# Prices an ad-hoc product built entirely from the request — no
# clients/<id>/products/<name>.yaml file needed. See
# engine/custom_pricing.py's module docstring for exactly which product
# types this can correctly price and which it deliberately refuses
# (annuities, non-life classes) rather than silently mispricing.

ANNUITY_PRODUCT_TYPES = {"annuity_immediate", "annuity_deferred"}
NONLIFE_PRODUCT_TYPES = {
    "motor_comprehensive", "motor_third_party", "fire", "burglary", "marine_cargo",
    "engineering", "liability", "travel", "bond", "aviation",
}

def _check_custom_product_type_supported(product_type: str) -> None:
    if product_type in ANNUITY_PRODUCT_TYPES:
        raise HTTPException(status_code=400, detail=(
            "Annuities aren't supported by the pricing engine yet. Annuity cash flows run in the "
            "opposite direction from every other product here (premium in, then survival-contingent "
            "payments out) and need a dedicated engine rather than a bolt-on to the death-benefit "
            "decrement loop — flagging this rather than pricing it incorrectly."
        ))
    if product_type in NONLIFE_PRODUCT_TYPES:
        raise HTTPException(status_code=400, detail=(
            "Non-life classes aren't supported by this pricing engine yet. General insurance "
            "pricing is loss-ratio/exposure based, not mortality-decrement based. AMVS already has "
            "a real non-life RESERVING engine (chain ladder / Bornhuetter-Ferguson — see "
            "engine/ifrs17_nonlife.py) but no non-life PRICING engine — flagging this rather than "
            "pricing it incorrectly."
        ))


class CustomRiderRequest(BaseModel):
    name:                    str             = Field(...)
    benefit_type:            str             = Field(..., description="death, tpd, critical_illness, hospital_cash, lump_sum_hospital, income_protection, funeral, maturity, or savings")
    benefit_amount:          float           = Field(..., ge=0)
    rider_term_years:        Optional[int]   = Field(None, ge=1, le=100, description="Defaults to the product's own policy term if omitted")
    waiting_period_months:   int             = Field(0, ge=0)
    annual_incidence_rate:   Optional[float] = Field(None, ge=0, le=1, description="Overrides the engine's default incidence rate for non-mortality-driven riders (tpd, critical_illness, hospital_cash, ...). Ignored for death/funeral (mortality-driven) and maturity/savings. Ignored if mortality_multiplier is set.")
    avg_events_per_year:     Optional[float] = Field(None, ge=0, description="Overrides the engine's default average events per year (e.g. nights per hospital admission). Ignored for death/funeral/maturity/savings.")
    mortality_multiplier:    Optional[float] = Field(None, ge=0, description="If set, this rider's frequency is this fixed multiple of the mortality rate at each age (e.g. 0.20 = TPD priced as 20% of the mortality rate) instead of a flat annual_incidence_rate — takes priority over annual_incidence_rate when both are set. Ignored for death/funeral/maturity/savings.")

class CustomDependantRequest(BaseModel):
    relationship:       str                    = Field(..., description="spouse, parent, child, or sibling")
    age:                Optional[int]           = Field(None, ge=0, le=110)
    benefit_overrides:  Dict[str, float]        = Field(default_factory=dict, description="Absolute benefit override per rider name — if a rider isn't listed here, the dependant gets that rider's full main-life benefit")

class CustomProductRequest(BaseModel):
    product_name:                  str                          = Field("Custom Product")
    product_type:                  str                           = Field("whole_life")
    policy_term_years:             Optional[int]                  = Field(None, ge=1, le=100, description="Omit for Whole Life")
    premium_payment_term_years:    Optional[int]                   = Field(None, ge=1, le=100)
    premium_mode:                  str                              = Field("monthly")
    sum_assured:                   float                             = Field(0, ge=0, description="Main benefit amount — always priced as its own rider, in addition to whatever is in `riders` below")
    entry_age:                     int                                = Field(35, ge=0, le=100)
    gender:                        str                                 = Field("unisex")
    riders:                        List[CustomRiderRequest]            = Field(default_factory=list, description="Additional riders/benefits layered on top of the main sum_assured benefit — not a replacement for it")
    dependants:                    List[CustomDependantRequest]         = Field(default_factory=list)
    assumptions:                   Optional[dict]                        = Field(None, description="Full AssumptionSet override — defaults to Ghana market defaults if omitted (a custom product has no saved basis of its own)")
    target_margin:                 Optional[float]                        = Field(None, ge=0, le=0.5)


# Which rider type sum_assured represents, per product_type — every type not
# listed here (annuities, non-life) never reaches _build_product_from_request
# because _check_custom_product_type_supported rejects it first. pure_endowment
# is handled separately below (survival-only — no death rider at all).
PRIMARY_BENEFIT_TYPE_BY_PRODUCT_TYPE: Dict[str, str] = {
    "whole_life": "death", "level_term": "death", "decreasing_term": "death",
    "endowment": "death", "educational_endowment": "death",
    "micro_life": "death", "group_life": "death",
    "hospital_cash": "hospital_cash", "medical_expense": "lump_sum_hospital",
    "critical_illness": "critical_illness", "income_protection": "income_protection",
}

# Endowment-family products pay the sum assured on survival to the end of
# the term TOO, not just on death — that's what actually makes an
# endowment an endowment rather than level term. Selecting "Endowment" in
# the dropdown didn't used to add this: it produced identical pricing to
# Level Term unless the user separately, manually added a Maturity rider.
ENDOWMENT_PRODUCT_TYPES = {"endowment", "educational_endowment"}


def _resolve_custom_assumptions(request: "CustomProductRequest") -> ProductAssumptions:
    assumption_set = AssumptionSet.from_dict(request.assumptions) if request.assumptions else AssumptionSet.ghana_defaults()
    base = ProductAssumptions(
        lapse_schedule=LapseSchedule(rates={1: 0.0}),
        entry_age_main=request.entry_age, gender_main_str=request.gender,
    )
    return assumption_set.to_product_assumptions(base=base, entry_age_main=request.entry_age)


def _build_product_from_request(request: "CustomProductRequest"):
    """
    sum_assured always becomes its own "Main Benefit" rider (when > 0) —
    it is never replaced by whatever's in `riders`, which are purely
    ADDITIONAL benefits (TPD, critical illness, hospital cash, ...) layered
    on top. Previously these were either/or (riders_or_default), which
    silently ignored sum_assured entirely whenever the dashboard's riders
    table had at least one row — see tests/test_custom_pricing.py's
    sum-assured regression tests for the failure mode this fixes.
    """
    spec = request.model_dump()
    riders = []
    user_has_maturity_rider = any(r.benefit_type == "maturity" for r in request.riders)

    if request.sum_assured > 0:
        if request.product_type == "pure_endowment":
            # Pays ONLY on survival to the end of term — no death cover at
            # all, unlike every other type here (the defining difference
            # between a pure endowment and an ordinary endowment).
            if request.policy_term_years:
                riders.append({
                    "name": "Maturity Value", "benefit_type": "maturity", "benefit_amount": request.sum_assured,
                    "rider_term_years": request.policy_term_years, "waiting_period_months": 0,
                })
        else:
            primary_type = PRIMARY_BENEFIT_TYPE_BY_PRODUCT_TYPE.get(request.product_type, "death")
            riders.append({
                "name": "Main Benefit", "benefit_type": primary_type, "benefit_amount": request.sum_assured,
                "rider_term_years": None, "waiting_period_months": 0,
            })
            if request.product_type in ENDOWMENT_PRODUCT_TYPES and request.policy_term_years and not user_has_maturity_rider:
                riders.append({
                    "name": "Maturity Value", "benefit_type": "maturity", "benefit_amount": request.sum_assured,
                    "rider_term_years": request.policy_term_years, "waiting_period_months": 0,
                })

    riders += [r.model_dump() for r in request.riders]
    spec["riders"] = riders
    return build_custom_product(spec)


@protected.post("/pricing/custom")
def pricing_custom_endpoint(request: CustomProductRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _check_custom_product_type_supported(request.product_type)
    if len(request.dependants) > 3:
        raise HTTPException(status_code=400, detail="A maximum of 3 dependants is supported.")
    try:
        product = _build_product_from_request(request)
        assumptions = _resolve_custom_assumptions(request)
        result = run_custom_pricing(product, assumptions, target_margin=request.target_margin, verbose=False)
        _log_valuation_run(db, current_user, "pricing_custom", request.model_dump(), result, client_id=None)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CustomRateTableRequest(CustomProductRequest):
    age_start: int = Field(18, ge=0, le=100)
    age_end:   int = Field(70, ge=0, le=100)

@protected.post("/pricing/custom/rate-table")
def pricing_custom_rate_table_endpoint(request: CustomRateTableRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _check_custom_product_type_supported(request.product_type)
    try:
        product = _build_product_from_request(request)
        assumptions = _resolve_custom_assumptions(request)
        table = run_custom_rate_table(product, assumptions, request.age_start, request.age_end, request.target_margin)
        result = {"rate_table": table, "assumptions_used": assumptions.to_dict(), "product_name": product.name}
        _log_valuation_run(db, current_user, "pricing_custom_rate_table", request.model_dump(), result, client_id=None)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CustomSensitivityRequest(CustomProductRequest):
    comprehensive: bool = Field(False, description="False (default) = the Pricing page's own small summary sweep; True = the full comprehensive sweep used by the standalone Sensitivity Analysis page (finer gradations, more assumption types, IFRS 17 impact per stress).")

@protected.post("/pricing/custom/sensitivity")
def pricing_custom_sensitivity_endpoint(request: CustomSensitivityRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _check_custom_product_type_supported(request.product_type)
    try:
        product = _build_product_from_request(request)
        assumptions = _resolve_custom_assumptions(request)
        stresses = SENSITIVITY_STRESSES if request.comprehensive else SENSITIVITY_STRESSES_SUMMARY
        sensitivity = run_custom_sensitivity(product, assumptions, request.target_margin, stresses=stresses)
        result = {"sensitivity": sensitivity, "assumptions_used": assumptions.to_dict(), "product_name": product.name}
        _log_valuation_run(db, current_user, "pricing_custom_sensitivity", request.model_dump(), result, client_id=None)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class InvestmentAnalysisRequest(CustomProductRequest):
    contributions_monthly: List[float] = Field(default_factory=lambda: [150.0, 300.0, 450.0, 600.0], description="Illustrative total monthly contribution levels (GHS) to project side by side — mirrors a real product illustration's comparison table.")
    credited_rate_pa: float = Field(0.05, gt=0, lt=1, description="Interest rate credited to the customer's savings fund per annum — distinct from the insurer's own investment return.")
    term_months: int = Field(24, ge=1, le=600)

@protected.post("/pricing/custom/investment-analysis")
def pricing_custom_investment_analysis_endpoint(request: InvestmentAnalysisRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _check_custom_product_type_supported(request.product_type)
    try:
        product = _build_product_from_request(request)
        assumptions = _resolve_custom_assumptions(request)
        pricing_result = run_custom_pricing(product, assumptions, target_margin=request.target_margin, verbose=False)
        risk_premium = pricing_result["monthly_premium"]

        funds = []
        for contribution in request.contributions_monthly:
            rows = project_investment_fund(contribution, risk_premium, request.credited_rate_pa, request.term_months)
            funds.append({
                "monthly_contribution": contribution,
                "monthly_projection": rows,
                "summary": summarise_investment_fund(rows),
            })

        result = {
            "risk_premium": risk_premium,
            "credited_rate_pa": request.credited_rate_pa,
            "term_months": request.term_months,
            "funds": funds,
            "product_name": product.name,
        }
        _log_valuation_run(db, current_user, "pricing_custom_investment_analysis", request.model_dump(), result, client_id=None)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@protected.post("/pricing/custom/export-excel")
def pricing_custom_export_excel_endpoint(request: CustomProductRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _check_custom_product_type_supported(request.product_type)
    try:
        product = _build_product_from_request(request)
        assumptions = _resolve_custom_assumptions(request)
        result = run_custom_pricing(product, assumptions, target_margin=request.target_margin, verbose=False)

        # The download button hands over one product spec, but a genuinely
        # useful export bundles everything the dashboard itself can show for
        # it — rate table, the full comprehensive sensitivity sweep, and (for
        # endowment-type products only) the investment fund projection —
        # rather than making the user separately screenshot each tab.
        rate_table = run_custom_rate_table(product, assumptions, 18, 70, request.target_margin)
        sensitivity = run_custom_sensitivity(product, assumptions, request.target_margin, stresses=SENSITIVITY_STRESSES)
        investment_analysis = None
        if request.product_type in ("endowment", "educational_endowment", "pure_endowment"):
            risk_premium = result["monthly_premium"]
            funds = []
            for contribution in (150.0, 300.0, 450.0, 600.0):
                rows = project_investment_fund(contribution, risk_premium, 0.05, 24)
                funds.append({"monthly_contribution": contribution, "monthly_projection": rows, "summary": summarise_investment_fund(rows)})
            investment_analysis = {"risk_premium": risk_premium, "credited_rate_pa": 0.05, "term_months": 24, "funds": funds, "product_name": product.name}

        excel_path = export_custom_pricing_to_excel(result, request.model_dump(), rate_table=rate_table, sensitivity=sensitivity, investment_analysis=investment_analysis)
        filename = os.path.basename(excel_path)
        response = {"excel_download_url": f"/pricing/custom/export-excel/download/{filename}"}
        _log_valuation_run(db, current_user, "pricing_custom_export_excel", request.model_dump(), response, client_id=None)
        return {"success": True, "data": response}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@protected.get("/pricing/custom/export-excel/download/{filename}")
def download_custom_pricing_excel(filename: str):
    safe_name = os.path.basename(filename)
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = os.path.join(CUSTOM_PRICING_EXCEL_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found — it may have been generated in a different session")
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=safe_name)


@protected.post("/pricing/custom/export-word")
def pricing_custom_export_word_endpoint(request: CustomProductRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _check_custom_product_type_supported(request.product_type)
    try:
        product = _build_product_from_request(request)
        assumptions = _resolve_custom_assumptions(request)
        result = run_custom_pricing(product, assumptions, target_margin=request.target_margin, verbose=False)
        docx_path = generate_custom_pricing_avr_note(result, request.model_dump())
        filename = os.path.basename(docx_path)
        response = {"word_download_url": f"/pricing/custom/export-word/download/{filename}"}
        _log_valuation_run(db, current_user, "pricing_custom_export_word", request.model_dump(), response, client_id=None)
        return {"success": True, "data": response}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@protected.get("/pricing/custom/export-word/download/{filename}")
def download_custom_pricing_word(filename: str):
    safe_name = os.path.basename(filename)
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = os.path.join(CUSTOM_PRICING_WORD_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found — it may have been generated in a different session")
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=safe_name)


class ActuarialMemoNarrative(BaseModel):
    policy_overview:              str = Field("", description="Free text — falls back to an auto-composed paragraph from the product spec if left blank")
    proceeds_and_settlement:      str = Field("")
    premium_notes:                str = Field("")
    underwriting_notes:           str = Field("")
    lapsation_and_reinstatement:  str = Field("")
    closing_notes:                str = Field("")


class ActuarialMemoRequest(CustomProductRequest):
    narrative:          ActuarialMemoNarrative = Field(default_factory=ActuarialMemoNarrative)
    appointed_actuary:  str = Field("Charles Osei-Akoto, ASA, MAAA")
    consulting_firm:    str = Field("Stallion Consultants Ltd")
    age_start:          int = Field(18, ge=0, le=100)
    age_end:            int = Field(70, ge=0, le=100)


@protected.post("/pricing/custom/export-memo")
def pricing_custom_export_memo_endpoint(request: ActuarialMemoRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _check_custom_product_type_supported(request.product_type)
    try:
        product = _build_product_from_request(request)
        assumptions = _resolve_custom_assumptions(request)
        result = run_custom_pricing(product, assumptions, target_margin=request.target_margin, verbose=False)
        rate_table = run_custom_rate_table(product, assumptions, request.age_start, request.age_end, request.target_margin)
        docx_path = generate_actuarial_memorandum(
            result, rate_table, request.model_dump(), request.narrative.model_dump(),
            appointed_actuary=request.appointed_actuary, consulting_firm=request.consulting_firm,
            product=product, assumptions=assumptions,
        )
        filename = os.path.basename(docx_path)
        response = {"memo_download_url": f"/pricing/custom/export-memo/download/{filename}"}
        _log_valuation_run(db, current_user, "pricing_custom_export_memo", request.model_dump(), response, client_id=None)
        return {"success": True, "data": response}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@protected.get("/pricing/custom/export-memo/download/{filename}")
def download_custom_pricing_memo(filename: str):
    safe_name = os.path.basename(filename)
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = os.path.join(CUSTOM_PRICING_MEMO_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found — it may have been generated in a different session")
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=safe_name)


@protected.post("/reserving")
def reserving_endpoint(request: ReservingRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        result = run_reserving(
            class_of_business         = request.class_of_business,
            gross_triangle            = request.gross_triangle,
            net_triangle              = request.net_triangle,
            method                    = request.method,
            gross_premium             = request.gross_premium,
            net_premium               = request.net_premium,
            expected_loss_ratio_gross = request.expected_loss_ratio_gross,
            expected_loss_ratio_net   = request.expected_loss_ratio_net,
            verbose                   = False,
        )
        _log_valuation_run(db, current_user, "reserving", request.model_dump(), result)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@protected.get("/reserving/nic-summary")
def reserving_nic_summary_endpoint(client_id: str = DEFAULT_CLIENT, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _require_data_access(client_id)
    try:
        result = run_nic_summary(client_id=client_id, verbose=False)
        _log_valuation_run(db, current_user, "nic_summary", {"client_id": client_id}, result, client_id=client_id)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
#  RESERVING NOTES — manual actuarial review/override, per client + class
# ══════════════════════════════════════════════════════════════════════════════
# Deliberately separate from the engine: run_nic_summary()'s own computed
# IBNR is never overwritten by these. This exists so a class flagged via
# ClientConfig.class_warnings (e.g. QIC's Accident — see
# clients/qic/assumptions.yaml) has somewhere for an actuary to record a
# reviewed figure and the reasoning behind it, preserving that as a human
# judgement call rather than the engine silently guessing at one. One note
# per (client_id, class_of_business) — saving again overwrites the prior
# note for that pair, there is no version history (see db.models.ReservingNote).

class ReservingNoteRequest(BaseModel):
    client_id:           str             = Field(..., description="Which insurer — see GET /clients")
    class_of_business:   str             = Field(..., description="e.g. Motor, Fire, Accident, Others")
    narrative:           str             = Field(..., min_length=1, description="Actuarial narrative — why this class needed review and what was concluded")
    adjusted_gross_ibnr: Optional[float] = Field(None, description="Optional — the actuary's own reviewed Gross IBNR figure, if the review concludes an override is warranted")
    adjusted_net_ibnr:   Optional[float] = Field(None, description="Optional — same, Net")

def _reserving_note_to_dict(note: ReservingNote, db: Session) -> dict:
    author = db.query(User).filter(User.id == note.user_id).first()
    return {
        "id":                  note.id,
        "client_id":           note.client_id,
        "class_of_business":   note.class_of_business,
        "narrative":           note.narrative,
        "adjusted_gross_ibnr": note.adjusted_gross_ibnr,
        "adjusted_net_ibnr":   note.adjusted_net_ibnr,
        "updated_by":          author.email if author else None,
        "updated_at":          note.updated_at.isoformat() if note.updated_at else None,
    }

@protected.get("/reserving/notes")
def list_reserving_notes(client_id: str = DEFAULT_CLIENT, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    notes = db.query(ReservingNote).filter(ReservingNote.client_id == client_id).all()
    return {"success": True, "data": [_reserving_note_to_dict(n, db) for n in notes]}

@protected.put("/reserving/notes")
def upsert_reserving_note(request: ReservingNoteRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    note = (
        db.query(ReservingNote)
        .filter(ReservingNote.client_id == request.client_id, ReservingNote.class_of_business == request.class_of_business)
        .first()
    )
    if note is None:
        note = ReservingNote(client_id=request.client_id, class_of_business=request.class_of_business, user_id=current_user.id)
        db.add(note)
    note.narrative           = request.narrative
    note.adjusted_gross_ibnr = request.adjusted_gross_ibnr
    note.adjusted_net_ibnr   = request.adjusted_net_ibnr
    note.user_id             = current_user.id
    db.commit()
    db.refresh(note)
    return {"success": True, "data": _reserving_note_to_dict(note, db)}


# ══════════════════════════════════════════════════════════════════════════════
#  NON-LIFE PAA STATEMENTS (Phase 2)
# ══════════════════════════════════════════════════════════════════════════════

class NonLifeStatementsRequest(BaseModel):
    client_id:                 str             = Field("pic", description="Which insurer — see clients/<client_id>/client.yaml. Use GET /clients to list configured clients.")
    period:                     str             = Field("FY2025")
    ra_loading:                   Optional[float] = Field(None, ge=0, le=1, description="Risk adjustment as a fraction of best-estimate IBNR+OCR; defaults to 15% if omitted")
    discount_duration_years:        Optional[float] = Field(None, ge=0, description="Assumed average claim-payment duration for discounting; defaults to 1.5 years if omitted")
    use_discounting:                  bool            = Field(True, description="Set False to skip NIC yield curve discounting entirely")

def _class_liability_to_dict(c) -> dict:
    return {
        "class_of_business": c.class_of_business, "basis": c.basis,
        "ibnr": c.ibnr, "ocr": c.ocr, "ulae": c.ulae, "upr": c.upr, "dac": c.dac,
        "effect_of_discounting": c.effect_of_discounting, "risk_adjustment": c.risk_adjustment,
        "lic": c.lic, "lrc": c.lrc, "is_onerous": c.is_onerous,
        "loss_component": c.loss_component, "total_liability": c.total_liability,
    }

@protected.post("/nonlife/statements")
def nonlife_statements_endpoint(request: NonLifeStatementsRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if request.client_id not in list_clients():
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown client_id '{request.client_id}' — "
                f"available clients: {list_clients()}"
            ),
        )
    _require_data_access(request.client_id)
    try:
        client = load_client(request.client_id)
        kwargs = {
            "client_id":      request.client_id,
            "period":         request.period,
            "use_discounting": request.use_discounting,
            "verbose":        False,
        }
        if request.ra_loading is not None:
            kwargs["ra_loading"] = request.ra_loading
        if request.discount_duration_years is not None:
            kwargs["discount_duration_years"] = request.discount_duration_years

        statements = generate_nonlife_paa_statements(**kwargs)
        paid       = load_paid_claims(client_id=request.client_id)
        entries    = generate_nonlife_journal(statements, paid, period=request.period)

        excel_path = export_nonlife_statements_to_excel(
            statements, entries,
            meta={"company_name": client.name, "data_source": f"{client.name} Data Summaries — {request.period}"},
        )
        filename = os.path.basename(excel_path)

        by_class = {
            cls: {basis: _class_liability_to_dict(statements["by_class"][cls][basis]) for basis in ("gross", "net", "ri")}
            for cls in statements["classes"]
        }
        totals = {basis: _class_liability_to_dict(statements["totals"][basis]) for basis in ("gross", "net", "ri")}

        response_data = {
                "period":                   statements["period"],
                "classes":                  statements["classes"],
                "by_class":                 by_class,
                "totals":                   totals,
                "ra_loading":               statements["ra_loading"],
                "discount_duration_years": statements["discount_duration_years"],
                "journal_entry_count":      len(entries),
                "journal_total_debit":      round(sum(e.debit for e in entries), 2),
                "journal_total_credit":     round(sum(e.credit for e in entries), 2),
                "excel_download_url":       f"/nonlife/statements/download/{filename}",
        }
        _log_valuation_run(
            db, current_user, "nonlife_statements", request.model_dump(), response_data,
            client_id=request.client_id,
        )
        return {"success": True, "data": response_data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@protected.get("/nonlife/statements/download/{filename}")
def download_nonlife_statements(filename: str):
    safe_name = os.path.basename(filename)   # reject any path traversal attempt
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = os.path.join(GENERATED_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found — it may have been generated in a different session")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=safe_name,
    )

@protected.get("/tiers")
def get_tiers():
    return {"success": True, "data": {"tiers": [
        {"tier": 1, "name": "Tier 1 — Basic",    "death_benefit": 5000,  "hosp_daily": 100, "funeral": 1000},
        {"tier": 2, "name": "Tier 2 — Standard", "death_benefit": 10000, "hosp_daily": 200, "funeral": 2000},
        {"tier": 3, "name": "Tier 3 — Premium",  "death_benefit": 20000, "hosp_daily": 400, "funeral": 4000},
    ]}}

@protected.get("/products")
def get_products():
    return {"success": True, "data": {"products": [
        {"id": "whole_life", "name": "Ghana Whole Life Insurance", "model": "GMM"},
        {"id": "micro_life", "name": "Ghana Micro Life Insurance", "model": "PAA"},
    ]}}

# ══════════════════════════════════════════════════════════════════════════════
#  NIC AVR REPORT ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════
from api.nic_report import generate_avr_data

class AVRRequest(BaseModel):
    company_name:      str   = Field(...,          description="Insurance company name")
    period:            str   = Field("FY2025",     description="Reporting period e.g. FY2025 or Q1 2026")
    reporting_freq:    str   = Field("annual",     description="annual or quarterly")
    in_force_count:    int   = Field(1000, ge=1,   description="Number of in-force policies")
    entry_age:         int   = Field(35, ge=18, le=70)
    tier:              int   = Field(1,  ge=1, le=3)
    appointed_actuary: str   = Field("Charles Osei-Akoto")
    consulting_firm:   str   = Field("Stallion Consultants Ltd")


@protected.post("/nic/avr")
def generate_avr(request: AVRRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        avr = generate_avr_data(
            company_name      = request.company_name,
            period            = request.period,
            reporting_freq    = request.reporting_freq,
            in_force_count    = request.in_force_count,
            entry_age         = request.entry_age,
            tier              = request.tier,
            appointed_actuary = request.appointed_actuary,
            consulting_firm   = request.consulting_firm,
        )
        _log_valuation_run(db, current_user, "avr", request.model_dump(), avr)
        return {"success": True, "data": avr}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
#  WORD (.docx) AVR EXPORT
# ══════════════════════════════════════════════════════════════════════════════
from outputs.word_exporter import generate_avr_word_document, GENERATED_DIR as WORD_GENERATED_DIR

class WordExportRequest(BaseModel):
    company_name:      str   = Field(...,          description="Insurance company name")
    period:            str   = Field("FY2025",     description="Reporting period e.g. FY2025 or Q1 2026")
    reporting_freq:    str   = Field("annual",     description="annual or quarterly")
    in_force_count:    int   = Field(1000, ge=1,   description="Number of in-force policies")
    entry_age:         int   = Field(35, ge=18, le=70)
    tier:              int   = Field(1,  ge=1, le=3)
    appointed_actuary: str   = Field("Charles Osei-Akoto")
    consulting_firm:   str   = Field("Stallion Consultants Ltd")
    client_id:         str   = Field("pic", description="Which insurer's non-life data to include — see GET /clients")
    include_nonlife:   bool  = Field(True, description="Include the 5.5 Non-life section (balance sheet, income statement, journal entries)")


@protected.post("/export/word")
def export_word_endpoint(request: WordExportRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if request.include_nonlife and request.client_id in list_clients():
        _require_data_access(request.client_id)
    try:
        avr = generate_avr_data(
            company_name      = request.company_name,
            period            = request.period,
            reporting_freq    = request.reporting_freq,
            in_force_count    = request.in_force_count,
            entry_age         = request.entry_age,
            tier              = request.tier,
            appointed_actuary = request.appointed_actuary,
            consulting_firm   = request.consulting_firm,
        )

        nonlife_statements, nonlife_entries, nonlife_client_name = None, None, None
        if request.include_nonlife and request.client_id in list_clients():
            client = load_client(request.client_id)
            nonlife_statements = generate_nonlife_paa_statements(
                client_id=request.client_id, period=request.period, verbose=False,
            )
            paid = load_paid_claims(client_id=request.client_id)
            nonlife_entries = generate_nonlife_journal(nonlife_statements, paid, period=request.period)
            nonlife_client_name = client.name

        docx_path = generate_avr_word_document(
            avr,
            nonlife_statements       = nonlife_statements,
            nonlife_journal_entries  = nonlife_entries,
            nonlife_client_name      = nonlife_client_name,
        )
        filename = os.path.basename(docx_path)

        response_data = {
                "word_download_url":  f"/export/word/download/{filename}",
                "nonlife_included":   nonlife_statements is not None,
        }
        _log_valuation_run(
            db, current_user, "word_export", request.model_dump(), response_data,
            client_id=request.client_id,
        )
        return {"success": True, "data": response_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@protected.get("/export/word/download/{filename}")
def download_word_export(filename: str):
    safe_name = os.path.basename(filename)   # reject any path traversal attempt
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = os.path.join(WORD_GENERATED_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found — it may have been generated in a different session")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=safe_name,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  EXCEL (.xlsx) IFRS 17 (LIFE / GMM) EXPORT
# ══════════════════════════════════════════════════════════════════════════════
from outputs.ifrs17_excel_exporter import export_ifrs17_to_excel, GENERATED_DIR as IFRS17_EXCEL_GENERATED_DIR


@protected.post("/export/excel")
def export_excel_endpoint(request: IFRS17Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """
    Life-side (GMM) counterpart to POST /nonlife/statements' Excel export —
    runs the same IFRS 17 valuation as POST /ifrs17 (same request shape,
    same shared _build_ifrs17_response_data() builder) and writes it to a
    formatted workbook instead of returning JSON.
    """
    try:
        response_data = _run_ifrs17_from_request(request)
        excel_path = export_ifrs17_to_excel(
            response_data, meta={"generated_at": datetime.now().strftime("%d %B %Y %H:%M")},
        )
        filename = os.path.basename(excel_path)

        result = {"excel_download_url": f"/export/excel/download/{filename}"}
        _log_valuation_run(db, current_user, "ifrs17_excel_export", request.model_dump(), result, client_id=DEFAULT_CLIENT)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@protected.get("/export/excel/download/{filename}")
def download_excel_export(filename: str):
    safe_name = os.path.basename(filename)   # reject any path traversal attempt
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = os.path.join(IFRS17_EXCEL_GENERATED_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found — it may have been generated in a different session")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=safe_name,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  WORD (.docx) NON-LIFE AVR EXPORT
# ══════════════════════════════════════════════════════════════════════════════
from outputs.nonlife_word_exporter import generate_nonlife_avr_word_document, GENERATED_DIR as NONLIFE_WORD_GENERATED_DIR


class NonLifeWordExportRequest(BaseModel):
    client_id:          str = Field("pic", description="Which insurer — see GET /clients")
    period:             str = Field("FY2025")
    appointed_actuary:  str = Field("Charles Osei-Akoto")
    consulting_firm:    str = Field("Stallion Consultants Ltd")


@protected.post("/export/nonlife-word")
def export_nonlife_word_endpoint(request: NonLifeWordExportRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """
    Full non-life NIC Actuarial Valuation Report (.docx), matching PIC's
    real published AVR structure exactly — see outputs/nonlife_word_exporter.py.
    """
    if request.client_id not in list_clients():
        raise HTTPException(
            status_code=400,
            detail=f"Unknown client_id '{request.client_id}' — available clients: {list_clients()}",
        )
    _require_data_access(request.client_id)
    try:
        docx_path = generate_nonlife_avr_word_document(
            client_id          = request.client_id,
            period              = request.period,
            appointed_actuary   = request.appointed_actuary,
            consulting_firm     = request.consulting_firm,
        )
        filename = os.path.basename(docx_path)

        result = {"word_download_url": f"/export/nonlife-word/download/{filename}"}
        _log_valuation_run(
            db, current_user, "nonlife_word_export", request.model_dump(), result,
            client_id=request.client_id,
        )
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@protected.get("/export/nonlife-word/download/{filename}")
def download_nonlife_word_export(filename: str):
    safe_name = os.path.basename(filename)   # reject any path traversal attempt
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = os.path.join(NONLIFE_WORD_GENERATED_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found — it may have been generated in a different session")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=safe_name,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  FILE UPLOAD — run the reserving engine against ad-hoc uploaded workbooks
# ══════════════════════════════════════════════════════════════════════════════
#
# Railway can't host a client's real Excel workbooks (see _require_data_access
# above) — this lets a user upload them directly through the dashboard instead
# of pre-configuring a client_id/data_folder. The engine still needs a
# *template* to know each workbook's expected filename, sheet names, and
# column layout (see engine/data_loader.py's module docstring) — "pic" is the
# only validated non-life workbook shape so far, so uploaded files must be
# named exactly as clients/pic/assumptions.yaml's data_files expects.
# client_name / valuation_date from the form are cosmetic report labels only
# (this doesn't register a new client) — the engine runs as
# client_id="pic" with its data folder swapped for the upload temp directory
# (data_folder_override — see engine/data_loader.py's _resolve_client()).

UPLOAD_TEMPLATE_CLIENT_ID = "pic"
_REQUIRED_UPLOAD_KEYS = ["ibnr_workbook", "raw_data_workbook", "upr_dac_workbook", "ulae_workbook"]


def _save_uploaded_files(files: List[UploadFile], temp_dir: str) -> None:
    for f in files:
        if not f.filename:
            continue
        if not f.filename.lower().endswith(".xlsx"):
            raise HTTPException(status_code=400, detail=f"'{f.filename}' is not a .xlsx file")
        dest = os.path.join(temp_dir, os.path.basename(f.filename))
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)


def _check_required_uploads(temp_dir: str) -> None:
    template = load_client(UPLOAD_TEMPLATE_CLIENT_ID)
    uploaded = {name.lower() for name in os.listdir(temp_dir)}
    missing = [template.data_files[k] for k in _REQUIRED_UPLOAD_KEYS if template.data_files[k].lower() not in uploaded]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                "Missing required file(s): " + "; ".join(missing) +
                " — uploaded files must be named exactly as PIC's own workbook template expects "
                "(the only validated non-life workbook shape so far)."
            ),
        )


def _run_uploaded_reserving(temp_dir: str, period: str):
    """Runs the full 4-class non-life PAA pipeline against uploaded workbooks. Returns (statements, journal_entries)."""
    statements = generate_nonlife_paa_statements(
        client_id=UPLOAD_TEMPLATE_CLIENT_ID, period=period, data_folder_override=temp_dir, verbose=False,
    )
    paid = load_paid_claims(client_id=UPLOAD_TEMPLATE_CLIENT_ID, data_folder_override=temp_dir)
    entries = generate_nonlife_journal(statements, paid, period=period)
    return statements, entries


def _journal_entry_to_dict(e) -> dict:
    return {
        "date": e.date, "account_code": e.account_code, "account_name": e.account_name,
        "debit": e.debit, "credit": e.credit, "narrative": e.narrative,
        "class_of_business": e.class_of_business, "basis": e.basis, "period": e.period,
    }


@protected.post("/upload/client-data")
async def upload_client_data(
    files:          List[UploadFile] = File(..., description="PIC-template non-life workbooks (.xlsx)"),
    client_name:    str              = Form(...),
    valuation_date: str              = Form("FY2025"),
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    temp_dir = tempfile.mkdtemp(prefix="amvs_upload_")
    try:
        _save_uploaded_files(files, temp_dir)
        _check_required_uploads(temp_dir)
        statements, entries = _run_uploaded_reserving(temp_dir, valuation_date)

        by_class = {
            cls: {basis: _class_liability_to_dict(statements["by_class"][cls][basis]) for basis in ("gross", "net", "ri")}
            for cls in statements["classes"]
        }
        totals = {basis: _class_liability_to_dict(statements["totals"][basis]) for basis in ("gross", "net", "ri")}

        response_data = {
            "client_name":          client_name,
            "period":               statements["period"],
            "classes":              statements["classes"],
            "by_class":             by_class,
            "totals":               totals,
            "reserving_summary":    statements["reserving_summary"],
            "journal_entries":      [_journal_entry_to_dict(e) for e in entries],
            "journal_total_debit":  round(sum(e.debit for e in entries), 2),
            "journal_total_credit": round(sum(e.credit for e in entries), 2),
        }
        _log_valuation_run(
            db, current_user, "upload_client_data",
            {"client_name": client_name, "period": valuation_date, "file_count": len(files)},
            response_data,
        )
        return {"success": True, "data": response_data}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@protected.post("/upload/export-excel")
async def upload_export_excel(
    files:          List[UploadFile] = File(...),
    client_name:    str              = Form(...),
    valuation_date: str              = Form("FY2025"),
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    temp_dir = tempfile.mkdtemp(prefix="amvs_upload_")
    try:
        _save_uploaded_files(files, temp_dir)
        _check_required_uploads(temp_dir)
        statements, entries = _run_uploaded_reserving(temp_dir, valuation_date)

        excel_path = export_nonlife_statements_to_excel(
            statements, entries,
            meta={"company_name": client_name, "data_source": f"{client_name} uploaded data — {valuation_date}"},
        )
        filename = os.path.basename(excel_path)
        result = {"excel_download_url": f"/nonlife/statements/download/{filename}"}
        _log_valuation_run(
            db, current_user, "upload_export_excel",
            {"client_name": client_name, "period": valuation_date, "file_count": len(files)},
            result,
        )
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@protected.post("/upload/export-word")
async def upload_export_word(
    files:              List[UploadFile] = File(...),
    client_name:        str              = Form(...),
    valuation_date:     str              = Form("FY2025"),
    appointed_actuary:  str              = Form("Charles Osei-Akoto"),
    consulting_firm:    str              = Form("Stallion Consultants Ltd"),
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    temp_dir = tempfile.mkdtemp(prefix="amvs_upload_")
    try:
        _save_uploaded_files(files, temp_dir)
        _check_required_uploads(temp_dir)

        docx_path = generate_nonlife_avr_word_document(
            client_id             = UPLOAD_TEMPLATE_CLIENT_ID,
            period                 = valuation_date,
            appointed_actuary       = appointed_actuary,
            consulting_firm           = consulting_firm,
            data_folder_override         = temp_dir,
            company_name_override           = client_name,
        )
        filename = os.path.basename(docx_path)
        result = {"word_download_url": f"/export/nonlife-word/download/{filename}"}
        _log_valuation_run(
            db, current_user, "upload_export_word",
            {"client_name": client_name, "period": valuation_date, "file_count": len(files)},
            result,
        )
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
#  SOLVENCY — GIRBC risk-based capital + legacy solvency margin (engine/rbc/)
# ══════════════════════════════════════════════════════════════════════════════
# Every request accepts EITHER client_id alone (auto-loads that client's real
# GIRBC/legacy workbooks via engine.data_loader.load_rbc_solvency_data — see
# that function's docstring) OR fully manual exposure inputs (what the
# dashboard's editable input panels send once a user has tweaked
# auto-loaded values, or for a client with no real workbook configured yet).
# Manual inputs win whenever ANY of them are provided — client_id alone
# only triggers auto-load when every input section is omitted.

class QualifyingCapitalResourcesRequest(BaseModel):
    tier1_unlimited:              float = 0.0
    tier1_unlimited_deductions:      float = 0.0
    tier1_limited:                      float = 0.0
    tier2:                                  float = 0.0
    tier2_amortisation:                        float = 0.0

class InsuranceRiskExposuresRequest(BaseModel):
    net_premium:           Dict[str, float] = Field(default_factory=dict)
    net_claims_reserve:       Dict[str, float] = Field(default_factory=dict)

class MarketRiskExposuresRequest(BaseModel):
    bonds_and_fixed_income:               Dict[str, float] = Field(default_factory=dict)
    listed_equities:                          Dict[str, float] = Field(default_factory=dict)
    real_estate:                                  Dict[str, float] = Field(default_factory=dict)
    right_of_use_assets:                              Dict[str, float] = Field(default_factory=dict)
    fx_net_open_position:                                 Dict[str, float] = Field(default_factory=dict)
    interest_rate_sensitive_assets:                            float = 0.0
    interest_rate_sensitive_liabilities:                           float = 0.0

class RatedExposureItem(BaseModel):
    amount:      float
    category:      str   # rating class (RC1-RC7/Unrated/Default) for counterparty_exposures, or LTV band for mortgage_exposures

class CreditRiskExposuresRequest(BaseModel):
    counterparty_exposures:      List[RatedExposureItem] = Field(default_factory=list)
    mortgage_exposures:              List[RatedExposureItem] = Field(default_factory=list)
    cash_and_deposits:                    float = 0.0
    premium_receivables:                     float = 0.0
    reinsurance_recoverables:                    float = 0.0
    mandatory_pool_recoverables:                     float = 0.0
    deferred_tax_assets:                                 float = 0.0
    related_party_loans:                                     float = 0.0
    other_receivables:                                           float = 0.0

class OperationalRiskExposuresRequest(BaseModel):
    current_year_net_premium:          float = 0.0
    prior_year_net_premium:                float = 0.0
    current_year_net_liabilities:              float = 0.0
    prior_year_net_liabilities:                    float = 0.0

class LegacySolvencyInputsRequest(BaseModel):
    total_capital_base:          float = 0.0
    capital_deductions:              float = 0.0
    asset_balances:                      Dict[str, float] = Field(default_factory=dict)
    net_written_premium:                    float = 0.0
    management_expenses:                        float = 0.0


def _rbc_inputs_provided(req) -> bool:
    return any([req.capital_resources, req.insurance_risk, req.market_risk, req.credit_risk, req.operational_risk])


def _to_capital_resources(req: Optional[QualifyingCapitalResourcesRequest]) -> QualifyingCapitalResources:
    return QualifyingCapitalResources(**req.model_dump()) if req else QualifyingCapitalResources()

def _to_insurance_exposures(req: Optional[InsuranceRiskExposuresRequest]) -> InsuranceRiskExposures:
    return InsuranceRiskExposures(**req.model_dump()) if req else InsuranceRiskExposures()

def _to_market_exposures(req: Optional[MarketRiskExposuresRequest]) -> MarketRiskExposures:
    return MarketRiskExposures(**req.model_dump()) if req else MarketRiskExposures()

def _to_credit_exposures(req: Optional[CreditRiskExposuresRequest]) -> CreditRiskExposures:
    if req is None:
        return CreditRiskExposures()
    d = req.model_dump()
    counterparty = [(item["amount"], item["category"]) for item in d.pop("counterparty_exposures")]
    mortgage = [(item["amount"], item["category"]) for item in d.pop("mortgage_exposures")]
    return CreditRiskExposures(counterparty_exposures=counterparty, mortgage_exposures=mortgage, **d)

def _to_operational_exposures(req: Optional[OperationalRiskExposuresRequest]) -> OperationalRiskExposures:
    return OperationalRiskExposures(**req.model_dump()) if req else OperationalRiskExposures()

def _to_legacy_inputs(req: Optional[LegacySolvencyInputsRequest]) -> LegacySolvencyInputs:
    return LegacySolvencyInputs(**req.model_dump()) if req else LegacySolvencyInputs()


def _resolve_rbc_exposures(req):
    """
    req must have: client_id, capital_resources, insurance_risk,
    net_non_life_insurance_revenue, market_risk, credit_risk,
    operational_risk (RbcSolvencyRequest and FullSolvencyRequest both do).

    Returns (capital_resources, insurance_exposures, net_non_life_insurance_revenue, market_exposures, credit_exposures, operational_exposures).
    """
    if req.client_id and not _rbc_inputs_provided(req):
        loaded = load_rbc_solvency_data(req.client_id)
        return (
            loaded["capital_resources"], loaded["insurance_risk"], loaded["net_non_life_insurance_revenue"],
            loaded["market_risk"], loaded["credit_risk"], loaded["operational_risk"],
        )
    return (
        _to_capital_resources(req.capital_resources), _to_insurance_exposures(req.insurance_risk),
        req.net_non_life_insurance_revenue, _to_market_exposures(req.market_risk),
        _to_credit_exposures(req.credit_risk), _to_operational_exposures(req.operational_risk),
    )


def _run_rbc_calculation(req):
    cap, ins_exp, revenue, mkt_exp, cred_exp, op_exp = _resolve_rbc_exposures(req)
    ins = calculate_insurance_risk(ins_exp, revenue)
    mkt = calculate_market_risk(mkt_exp)
    cred = calculate_credit_risk(cred_exp)
    op = calculate_operational_risk(op_exp)
    return calculate_solvency(ins, mkt, cred, op, cap)


def _capital_resources_dict(cap: QualifyingCapitalResources) -> dict:
    """
    dataclasses.asdict() only serialises DECLARED FIELDS — it silently
    drops @property values (net_tier1_unlimited, eligible_tier1_limited,
    eligible_tier2, total_qcr, composition_valid all live as properties on
    QualifyingCapitalResources, not fields — see data_model.py). Found via
    live browser testing: the dashboard's Capital Composition tab showed
    "0.0% of QCR" instead of the real percentage because of exactly this —
    asdict(sol)'s nested capital_resources dict was missing every computed
    value. This patches them back in wherever a SolvencyResult crosses the
    JSON boundary.
    """
    d = asdict(cap)
    d.update({
        "net_tier1_unlimited":      round(cap.net_tier1_unlimited, 2),
        "eligible_tier1_limited":      round(cap.eligible_tier1_limited, 2),
        "eligible_tier2":                 round(cap.eligible_tier2, 2),
        "total_qcr":                         round(cap.total_qcr, 2),
        "composition_valid":                    cap.composition_valid,
    })
    return d


def _solvency_result_dict(sol: SolvencyResult) -> dict:
    d = asdict(sol)
    d["capital_resources"] = _capital_resources_dict(sol.capital_resources)
    return d


def _resolve_legacy_inputs(client_id: Optional[str], req_inputs: Optional[LegacySolvencyInputsRequest]) -> Optional[LegacySolvencyInputs]:
    if client_id and req_inputs is None:
        loaded = load_rbc_solvency_data(client_id)
        return loaded["legacy_inputs"]
    return _to_legacy_inputs(req_inputs)


class RbcSolvencyRequest(BaseModel):
    client_id:                            Optional[str] = Field(None, description="Auto-loads this client's real GIRBC workbook if no manual inputs are given — see GET /clients")
    capital_resources:                        Optional[QualifyingCapitalResourcesRequest] = None
    insurance_risk:                               Optional[InsuranceRiskExposuresRequest] = None
    net_non_life_insurance_revenue:                   float = 0.0
    market_risk:                                          Optional[MarketRiskExposuresRequest] = None
    credit_risk:                                              Optional[CreditRiskExposuresRequest] = None
    operational_risk:                                             Optional[OperationalRiskExposuresRequest] = None

class LegacySolvencyRequest(BaseModel):
    client_id:          Optional[str] = None
    legacy_inputs:          Optional[LegacySolvencyInputsRequest] = None

class FullSolvencyRequest(BaseModel):
    client_id:                            Optional[str] = None
    valuation_date:                           str = "FY2025"
    capital_resources:                            Optional[QualifyingCapitalResourcesRequest] = None
    insurance_risk:                                   Optional[InsuranceRiskExposuresRequest] = None
    net_non_life_insurance_revenue:                       float = 0.0
    market_risk:                                              Optional[MarketRiskExposuresRequest] = None
    credit_risk:                                                  Optional[CreditRiskExposuresRequest] = None
    operational_risk:                                                 Optional[OperationalRiskExposuresRequest] = None
    legacy_inputs:                                                        Optional[LegacySolvencyInputsRequest] = None

class SaveSolvencyRequest(BaseModel):
    client_id:          str
    valuation_date:         str = "FY2025"
    inputs:                     dict
    results:                       dict
    girbc_car:                         Optional[float] = None
    legacy_car:                            Optional[float] = None


def _solvency_run_to_dict(run: SolvencyRun, db: Session) -> dict:
    author = db.query(User).filter(User.id == run.user_id).first()
    return {
        "id": run.id, "client_id": run.client_id, "valuation_date": run.valuation_date,
        "girbc_car": run.girbc_car, "legacy_car": run.legacy_car,
        "created_by": author.email if author else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "inputs": json.loads(run.inputs_json), "results": json.loads(run.results_json),
    }


@protected.post("/solvency/rbc")
def solvency_rbc_endpoint(request: RbcSolvencyRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        sol = _run_rbc_calculation(request)
        result = _solvency_result_dict(sol)
        _log_valuation_run(db, current_user, "solvency_rbc", request.model_dump(), result, client_id=request.client_id)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@protected.post("/solvency/legacy")
def solvency_legacy_endpoint(request: LegacySolvencyRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        inputs = _resolve_legacy_inputs(request.client_id, request.legacy_inputs)
        if inputs is None:
            raise HTTPException(status_code=400, detail=f"Client '{request.client_id}' has no legacy_workbook configured in rbc_data_files")
        result = asdict(calculate_legacy_solvency(inputs))
        _log_valuation_run(db, current_user, "solvency_legacy", request.model_dump(), result, client_id=request.client_id)
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@protected.post("/solvency/full")
def solvency_full_endpoint(request: FullSolvencyRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        cap, ins_exp, revenue, mkt_exp, cred_exp, op_exp = _resolve_rbc_exposures(request)
        ins = calculate_insurance_risk(ins_exp, revenue)
        mkt = calculate_market_risk(mkt_exp)
        cred = calculate_credit_risk(cred_exp)
        op = calculate_operational_risk(op_exp)
        sol = calculate_solvency(ins, mkt, cred, op, cap)
        girbc_result = _solvency_result_dict(sol)

        stress_results = [asdict(r) for r in run_stress_tests(ins_exp, revenue, mkt_exp, cred_exp, op_exp, cap)]

        legacy_inputs = _resolve_legacy_inputs(request.client_id, request.legacy_inputs)
        legacy_result = asdict(calculate_legacy_solvency(legacy_inputs)) if legacy_inputs is not None else None

        result = {
            "valuation_date": request.valuation_date, "girbc": girbc_result, "legacy": legacy_result,
            "stress_tests": stress_results,
        }
        _log_valuation_run(db, current_user, "solvency_full", request.model_dump(), result, client_id=request.client_id)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@protected.get("/solvency/raw-inputs/{client_id}")
def solvency_raw_inputs_endpoint(client_id: str, current_user: User = Depends(get_current_user)):
    """
    The RAW exposure inputs parsed from a client's real GIRBC/legacy
    workbooks (not the computed risk charges — see POST /solvency/rbc for
    that) — lets the dashboard's "Load from client data" button populate
    its editable input panels with real starting values a user can then
    review/adjust before running the actual calculation.
    """
    try:
        data = load_rbc_solvency_data(client_id)
        return {
            "success": True,
            "data": {
                "capital_resources": asdict(data["capital_resources"]),
                "insurance_risk": asdict(data["insurance_risk"]),
                "net_non_life_insurance_revenue": data["net_non_life_insurance_revenue"],
                "market_risk": asdict(data["market_risk"]),
                "credit_risk": {
                    **asdict(data["credit_risk"]),
                    "counterparty_exposures": [{"amount": a, "category": c} for a, c in data["credit_risk"].counterparty_exposures],
                    "mortgage_exposures": [{"amount": a, "category": c} for a, c in data["credit_risk"].mortgage_exposures],
                },
                "operational_risk": asdict(data["operational_risk"]),
                "legacy_inputs": asdict(data["legacy_inputs"]) if data["legacy_inputs"] is not None else None,
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@protected.get("/solvency/history/{client_id}")
def solvency_history_endpoint(client_id: str, limit: int = 50, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    runs = (
        db.query(SolvencyRun).filter(SolvencyRun.client_id == client_id)
        .order_by(SolvencyRun.created_at.desc()).limit(min(limit, 200)).all()
    )
    return {"success": True, "data": [_solvency_run_to_dict(r, db) for r in runs]}


@protected.post("/solvency/save")
def solvency_save_endpoint(request: SaveSolvencyRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        run = SolvencyRun(
            client_id=request.client_id, user_id=current_user.id, valuation_date=request.valuation_date,
            girbc_car=request.girbc_car, legacy_car=request.legacy_car,
            inputs_json=json.dumps(request.inputs), results_json=json.dumps(request.results),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return {"success": True, "data": _solvency_run_to_dict(run, db)}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


class SolvencyCertificateRequest(BaseModel):
    client_id:                            Optional[str] = None
    company_name:                             Optional[str] = Field(None, description="Overrides the client's configured name on the certificate")
    valuation_date:                               str = "FY2025"
    appointed_actuary:                                str = "Charles Osei-Akoto"
    qualifications:                                       str = "Fellow, Institute and Faculty of Actuaries (FIA)"
    consulting_firm:                                          str = "Stallion Consultants Ltd"
    capital_resources:                                            Optional[QualifyingCapitalResourcesRequest] = None
    insurance_risk:                                                   Optional[InsuranceRiskExposuresRequest] = None
    net_non_life_insurance_revenue:                                       float = 0.0
    market_risk:                                                              Optional[MarketRiskExposuresRequest] = None
    credit_risk:                                                                  Optional[CreditRiskExposuresRequest] = None
    operational_risk:                                                                 Optional[OperationalRiskExposuresRequest] = None
    legacy_inputs:                                                                        Optional[LegacySolvencyInputsRequest] = None


@protected.post("/export/solvency-certificate")
def export_solvency_certificate_endpoint(request: SolvencyCertificateRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """GIRBC solvency certificate (.docx) — CAR summary, risk breakdown, capital composition, stress tests, actuarial opinion, signature block."""
    try:
        cap, ins_exp, revenue, mkt_exp, cred_exp, op_exp = _resolve_rbc_exposures(request)
        ins = calculate_insurance_risk(ins_exp, revenue)
        mkt = calculate_market_risk(mkt_exp)
        cred = calculate_credit_risk(cred_exp)
        op = calculate_operational_risk(op_exp)
        sol = calculate_solvency(ins, mkt, cred, op, cap)

        legacy_inputs = _resolve_legacy_inputs(request.client_id, request.legacy_inputs)
        legacy_result = calculate_legacy_solvency(legacy_inputs) if legacy_inputs is not None else None

        stress_results = run_stress_tests(ins_exp, revenue, mkt_exp, cred_exp, op_exp, cap)

        display_name = request.company_name or (load_client(request.client_id).name if request.client_id else "Insurer")

        docx_path = generate_girbc_certificate_from_results(
            client_name=display_name, valuation_date=request.valuation_date, sol=sol, legacy_result=legacy_result,
            stress_results=stress_results, appointed_actuary=request.appointed_actuary,
            qualifications=request.qualifications, consulting_firm=request.consulting_firm,
        )
        filename = os.path.basename(docx_path)
        result = {"word_download_url": f"/export/solvency-certificate/download/{filename}"}
        _log_valuation_run(db, current_user, "solvency_certificate", request.model_dump(), result, client_id=request.client_id)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@protected.get("/export/solvency-certificate/download/{filename}")
def download_solvency_certificate(filename: str):
    safe_name = os.path.basename(filename)
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = os.path.join(SOLVENCY_WORD_GENERATED_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found — it may have been generated in a different session")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=safe_name,
    )


@protected.get("/nic/quarters")
def get_quarters():
    return {
        "success": True,
        "data": {
            "quarters": [
                {"quarter": "Q1", "period_end": "31 March",      "submission_deadline": "30 April",   "label": "Q1 2026"},
                {"quarter": "Q2", "period_end": "30 June",       "submission_deadline": "31 July",    "label": "Q2 2026"},
                {"quarter": "Q3", "period_end": "30 September",  "submission_deadline": "31 October", "label": "Q3 2026"},
                {"quarter": "Q4", "period_end": "31 December",   "submission_deadline": "31 January", "label": "Q4 2025 / Annual"},
            ]
        }
    }


# All routes registered on `protected` above require a valid bearer token —
# see auth/dependencies.py's get_current_user.
app.include_router(protected, dependencies=[Depends(get_current_user)])