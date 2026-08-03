import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()   # loads .env for local dev — no-op if it doesn't exist (Railway injects env vars directly)

from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import shutil
import tempfile

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Dict, List, Optional
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from engine.runner import run_pricing, run_ifrs17, run_rate_table, run_reserving, run_nic_summary
from engine.ifrs17_nonlife import generate_nonlife_paa_statements
from engine.data_loader import load_paid_claims
from engine.journals import generate_nonlife_journal
from engine.clients import list_clients, load_client
from outputs.excel_exporter import export_nonlife_statements_to_excel, GENERATED_DIR

from db.database import DATABASE_URL, SessionLocal, engine as db_engine, get_db, init_db
from db.models import User, ValuationRun
from auth.dependencies import get_current_user
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
    entry_age:         int   = Field(35,   ge=18, le=70)
    tier:              int   = Field(1,    ge=1,  le=3)
    product:           str   = Field("whole_life")
    dependant:         str   = Field("spouse")
    mortality_loading: float = Field(-0.20)
    target_margin:     float = Field(0.15, ge=0,  le=0.5)

class IFRS17Request(BaseModel):
    company_name:    str   = Field(...)
    product_type:    str   = Field("whole_life")
    period:          str   = Field("FY2025")
    period_index:    int   = Field(1, ge=1)
    in_force_count:  int   = Field(1000, ge=1)
    entry_age:       int   = Field(35,   ge=18, le=70)
    tier:            int   = Field(1,    ge=1,  le=3)
    reporting_freq:  str   = Field("annual")

class RateTableRequest(BaseModel):
    tier:      int = Field(1,  ge=1,  le=3)
    product:   str = Field("whole_life")
    age_start: int = Field(18, ge=18, le=70)
    age_end:   int = Field(70, ge=18, le=70)

class ReservingRequest(BaseModel):
    class_of_business:          str                     = Field(..., description="e.g. Motor, Fire, Accident, Others")
    gross_triangle:             Dict[int, List[float]]  = Field(..., description="origin_year -> cumulative incurred by development age")
    net_triangle:                Dict[int, List[float]] = Field(..., description="same shape, net of reinsurance")
    method:                       str                    = Field("chain_ladder", description="chain_ladder or bornhuetter_ferguson")
    gross_premium:                Optional[Dict[int, float]] = Field(None, description="required if method=bornhuetter_ferguson")
    net_premium:                   Optional[Dict[int, float]] = Field(None, description="required if method=bornhuetter_ferguson")
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

@protected.post("/pricing")
def pricing_endpoint(request: PricingRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        result = run_pricing(
            client_id          = DEFAULT_CLIENT,
            product_name       = _resolve_product_name(request.product, request.tier),
            entry_age          = request.entry_age,
            mortality_loading  = request.mortality_loading,
            target_margin      = request.target_margin,
            verbose            = False,
        )
        _log_valuation_run(db, current_user, "pricing", request.model_dump(), result, client_id=DEFAULT_CLIENT)
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
    }


def _run_ifrs17_from_request(request: "IFRS17Request") -> dict:
    report = run_ifrs17(
        client_id       = DEFAULT_CLIENT,
        product_name    = _resolve_product_name(request.product_type, request.tier),
        company_name    = request.company_name,
        period          = request.period,
        period_index    = request.period_index,
        in_force_count  = request.in_force_count,
        entry_age       = request.entry_age,
        reporting_freq  = request.reporting_freq,
        verbose         = False,
    )
    return _build_ifrs17_response_data(report)


@protected.post("/ifrs17")
def ifrs17_endpoint(request: IFRS17Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        response_data = _run_ifrs17_from_request(request)
        _log_valuation_run(db, current_user, "ifrs17", request.model_dump(), response_data, client_id=DEFAULT_CLIENT)
        return {"success": True, "data": response_data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@protected.post("/rate-table")
def rate_table_endpoint(request: RateTableRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        result = run_rate_table(
            client_id     = DEFAULT_CLIENT,
            product_name  = _resolve_product_name(request.product, request.tier),
            age_start     = request.age_start,
            age_end       = request.age_end,
            verbose       = False,
        )
        _log_valuation_run(db, current_user, "rate_table", request.model_dump(), result, client_id=DEFAULT_CLIENT)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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