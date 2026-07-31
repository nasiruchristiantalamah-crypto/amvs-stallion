# AMVS — Ghana Actuarial Modelling & Valuation System

Actuarial pricing, IFRS 17 valuation, and NIC reporting for Stallion
Consultants Ltd's insurer clients. FastAPI backend, SQLAlchemy/PostgreSQL
for auth and audit history, YAML-based client/product/assumption config.

## Running locally

```
pip install -r requirements.txt
cp .env.example .env    # fill in SECRET_KEY at minimum — see .env.example
uvicorn api.main:app --reload
```

Without `DATABASE_URL` set, the API falls back to a local SQLite file
(`amvs_dev.db`) so it boots without Postgres running — fine for
development, not for production (see `db/database.py`).

The first user you register (`POST /auth/register`) automatically becomes
an admin — that's the only way to bootstrap a brand-new database. Every
registration after that requires an admin token. See `auth/router.py` and
`scripts/create_admin.py` (a command-line alternative that bypasses the
API entirely, useful for recovering a locked-out deployment).

Run the test suite:

```
pytest tests/ -q
```

## Deploying to Railway

1. Push this repository (the `amvs/` folder — see below) to GitHub and
   connect it to a new Railway service.
2. Attach a PostgreSQL plugin — Railway injects `DATABASE_URL`
   automatically.
3. Set the remaining environment variables under the service's Variables
   tab (see `.env.example` for the full list): `SECRET_KEY` (required —
   generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"`),
   `ALLOWED_ORIGINS`, and optionally `ACCESS_TOKEN_EXPIRE_MINUTES`.
4. Deploy. Railway runs the `Procfile`'s `web` command
   (`uvicorn api.main:app --host 0.0.0.0 --port $PORT`).

### Non-life client data — read this before expecting `/reserving/*`, `/nonlife/*`, or `/export/word` to work on Railway

Each client's non-life source workbooks (claims triangles, outstanding
claims register, UPR/DAC, ULAE — see `engine/data_loader.py`) are real
Excel files that live on an analyst's laptop, referenced by an absolute
local path. That path is **not** committed to git (`clients/<id>/client.yaml`
is gitignored — see `.gitignore` and `engine/clients.py`'s module
docstring), and Railway has nowhere to put the real files either — they're
private, often large, and specific to whoever's running the valuation.

Instead, `engine/clients.py`'s `load_client()` resolves each client's data
folder from an environment variable named `<CLIENT_ID upper-cased>_DATA_DIR`:

| Client | Environment variable |
|---|---|
| `pic` | `PIC_DATA_DIR` |
| `qic` | `QIC_DATA_DIR` |

If you're onboarding a new client, add its own `<CLIENT_ID>_DATA_DIR`
following the same pattern (see `clients/_template/`).

**On Railway, these are not set by default**, because there's no server
filesystem to point them at (uploading a client's confidential workbooks
to a shared cloud disk isn't something this system does — that needs a
proper deliberate decision about where that data is allowed to live, not
a default). Until you decide how to get real workbooks onto (or reachable
from) the Railway instance — a mounted volume, a private object-storage
bucket the app reads from at request time, etc. — leave the data-dir
variables unset.

**What still works without them:**
- `POST /pricing`, `POST /ifrs17`, `POST /rate-table` — the life side reads
  entirely from `clients/<id>/products/*.yaml` and
  `clients/<id>/assumptions/*.yaml`, both committed to git. No data folder
  needed.
- `POST /nic/avr` — the life sections generate normally; section 5.5
  (non-life claims reserves) reports itself `"available": false` with an
  explanatory message instead of failing the whole report.

**What returns HTTP 503 instead of a data folder it doesn't have:**
- `GET /reserving/nic-summary`
- `POST /nonlife/statements`
- `POST /export/word` (only when `include_nonlife: true` and `client_id`
  is a configured client — set `include_nonlife: false` to skip this)

The response body is:

```json
{"detail": "Data files not available on this server — contact Stallion Consultants to arrange data access"}
```

not a raw file-not-found error — see `api/main.py`'s `_require_data_access()`
and `engine/clients.py`'s `ClientConfig.data_dir_available`.

### Repository root for Railway

The actual project (this README, `Procfile`, `requirements.txt`, `api/`,
`engine/`, etc.) lives in the `amvs/` subfolder of the wider working
directory this was developed in, not its parent. If your Railway project's
git root ends up being the parent folder instead, set the service's **Root
Directory** to `amvs` in Railway's settings so it finds the `Procfile`.

## Project layout

```
api/        FastAPI app, request models, NIC AVR report assembly
auth/       JWT auth — password hashing, token creation/verification, /auth/* routes
db/         SQLAlchemy models (users, clients, valuation_runs) and session setup
engine/     Actuarial calculation core — pricing, IFRS 17 (GMM + PAA), reserving
data/       Mortality tables, yield curves, shared validation helpers
outputs/    Excel and Word report exporters
clients/    Per-client config — see clients/_template/ for onboarding a new one
scripts/    Command-line ops utilities (e.g. scripts/create_admin.py)
tests/      pytest suite, validated against clients' real historical figures where available
```
