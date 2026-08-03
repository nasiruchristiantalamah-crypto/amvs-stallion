"""
================================================================================
DATABASE MODELS — users, clients, valuation_runs
================================================================================
What this file does:
    The three SQLAlchemy tables backing production auth and audit history:

        User          — login credentials + role (admin/client), one row
                        per person who can sign in to the API.
        Client         — lightweight database registry of which client_ids
                        exist, distinct from (but referencing) the
                        file-based config in clients/<client_id>/ that
                        engine/clients.py reads (data folder, assumption
                        overrides, product definitions). This table exists
                        so users and valuation_runs can carry a real
                        foreign key to "which insurer" instead of a bare
                        string.
        ValuationRun   — an audit log entry for every pricing/valuation/
                        reserving run executed through the API: who ran
                        it, for which client, with what inputs, producing
                        what outputs. Inputs/outputs are stored as JSON
                        exactly as the engine received/returned them, so a
                        run can be inspected or diffed later without
                        re-running the engine. `inputs` includes the full
                        assumptions JSON actually used for the run (see
                        engine/assumptions_manager.py's AssumptionSet) —
                        the audit trail for "what basis produced this number".
        AssumptionSet  — a named, saved bundle of pricing/valuation
                        assumptions a user built on the dashboard's
                        Assumptions page (engine/assumptions_manager.py's
                        AssumptionSet dataclass, serialised to JSON in
                        assumptions_json). Lets a user re-apply the same
                        named basis ("PIC Pricing Basis 2025", "Stressed
                        Assumptions") across multiple runs without
                        re-entering every field each time.
        ReservingNote  — an actuary's manual case-reserve-adequacy review
                        for one client/class of business on the non-life
                        Reserving page: a required narrative, plus optional
                        adjusted_gross_ibnr/adjusted_net_ibnr the actuary
                        can record as their own reviewed figure. This is
                        deliberately NOT applied automatically anywhere —
                        engine.runner.run_nic_summary()'s own computed
                        figure is never overwritten by it. It exists so a
                        flagged class (see ClientConfig.class_warnings,
                        e.g. QIC's Accident class) has somewhere to record
                        the human judgement call instead of the engine
                        silently guessing at one. One row per (client_id,
                        class_of_business) — saving again overwrites the
                        prior note for that pair; there is no version
                        history.
================================================================================
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Enum as SAEnum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from db.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserRole(str, enum.Enum):
    ADMIN  = "admin"
    CLIENT = "client"


class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    email           = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    company_name    = Column(String(255), nullable=False)
    role            = Column(SAEnum(UserRole, native_enum=False, length=16), nullable=False, default=UserRole.CLIENT)
    # Which clients/<client_id>/ this user belongs to — populated for
    # role="client" users (an insurer's own staff, scoped to their own
    # data); left null for role="admin" users (Stallion staff, see everything).
    client_id       = Column(String(64), nullable=True, index=True)
    is_active       = Column(Boolean, nullable=False, default=True)
    created_at      = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    valuation_runs = relationship("ValuationRun", back_populates="user")


class Client(Base):
    __tablename__ = "clients"

    id          = Column(Integer, primary_key=True, index=True)
    client_id   = Column(String(64), unique=True, index=True, nullable=False)  # matches clients/<client_id>/ folder name
    name        = Column(String(255), nullable=False)
    currency    = Column(String(8), nullable=False, default="GHS")
    is_active   = Column(Boolean, nullable=False, default=True)
    created_at  = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ValuationRun(Base):
    __tablename__ = "valuation_runs"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    client_id   = Column(String(64), nullable=True, index=True)
    run_type    = Column(String(64), nullable=False, index=True)  # "pricing", "ifrs17", "reserving", "nonlife_statements", "avr", "word_export"
    inputs      = Column(JSON, nullable=False)
    outputs     = Column(JSON, nullable=True)
    created_at  = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    user = relationship("User", back_populates="valuation_runs")


class AssumptionSet(Base):
    __tablename__ = "assumption_sets"

    id                = Column(Integer, primary_key=True, index=True)
    name              = Column(String(255), nullable=False)
    description       = Column(String(1000), nullable=True)
    client_id         = Column(String(64), nullable=True, index=True)
    user_id           = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    is_default        = Column(Boolean, nullable=False, default=False)
    created_at        = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at        = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    # Full engine.assumptions_manager.AssumptionSet.to_dict() output, as JSON
    # text — stored as Text (not the JSON column type) so it round-trips
    # identically on both SQLite (dev) and Postgres (prod) without either
    # engine re-normalising key order/types.
    assumptions_json  = Column(Text, nullable=False)

    user = relationship("User")


class ReservingNote(Base):
    __tablename__ = "reserving_notes"
    __table_args__ = (
        UniqueConstraint("client_id", "class_of_business", name="uq_reserving_note_client_class"),
    )

    id                  = Column(Integer, primary_key=True, index=True)
    client_id           = Column(String(64), nullable=False, index=True)   # e.g. "pic", "qic" — see engine/clients.py
    class_of_business   = Column(String(64), nullable=False, index=True)   # e.g. "Motor", "Fire", "Accident", "Others"
    narrative           = Column(Text, nullable=False)
    # Optional — the actuary's own reviewed figure, if the review concludes
    # the engine's computed IBNR should be overridden. Left null when the
    # note is purely a review record with no numeric override.
    adjusted_gross_ibnr = Column(Float, nullable=True)
    adjusted_net_ibnr   = Column(Float, nullable=True)
    user_id             = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at          = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at          = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    user = relationship("User")
