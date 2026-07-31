"""
================================================================================
CLIENT REGISTRY
================================================================================
What this file does:
    AMVS is no longer hardcoded to one insurer. Every client (insurer) has
    its own folder under clients/<client_id>/, split into two files that
    are deliberately kept separate because they have different sensitivity
    and different lifecycles:

        assumptions.yaml   — name, currency, fx rate, non-life workbook
                             filenames (data_files — different clients'
                             analysts name and lay out their workbooks
                             differently, even when the underlying sheets
                             are the same shape; see engine/data_loader.py),
                             assumption overrides, and reinsurance
                             structure. Contains nothing machine-specific
                             — SAFE to commit to git, and required: this is
                             the file that makes a client "exist" (see
                             list_clients()).
        client.yaml         — OPTIONAL, local-development-only. Just
                             data_folder (the absolute path to this
                             client's real Excel workbooks) and
                             results_folder. Gitignored — these are real
                             paths into a specific analyst's OneDrive, not
                             portable, and not needed at all once a client
                             is deployed (see below).

    In production (Railway), there's nowhere to put a client's real Excel
    workbooks — they're private, large, and live on someone's laptop. The
    data folder there comes from an environment variable instead:
    PIC_DATA_DIR, QIC_DATA_DIR, etc. (<CLIENT_ID upper-cased>_DATA_DIR).
    Precedence in load_client(): env var, if set, wins; otherwise fall back
    to client.yaml's data_folder (local dev); otherwise data_folder is None
    and non-life endpoints for that client 503 rather than crash (see
    ClientConfig.data_dir_available and api/main.py's non-life routes).

    Also under clients/<client_id>/:
        products/<name>.yaml   — product definitions (engine/product.py's Product)
        assumptions/<product>/ — versioned pricing/valuation assumptions
                                 (engine/assumptions_store.py — NOT to be
                                 confused with this file's assumptions.yaml,
                                 which is client-level, not per-product)

    This is the entry point everything else in the engine goes through:
    "which client, which product" is resolved here before assumptions,
    non-life data, or reporting touch anything. See clients/_template/ for
    a blank config to copy when onboarding a new client.
================================================================================
"""

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from engine.product import Product

CLIENTS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "clients")

# Default non-life workbook filenames/sheet names — PIC's own convention.
# Any client whose analysts name files differently overrides only the keys
# that differ in their assumptions.yaml's data_files: section.
DEFAULT_DATA_FILES = {
    "ibnr_workbook":      "2025 IBNR Projection (Gross & Net) - Final.xlsx",
    "raw_data_workbook":  "2025 PIC Final Data.xlsx",
    "upr_dac_workbook":   "UPR & DAC (2025).xlsx",
    "upr_dac_sheet":      "UPR & DAC",
    "ulae_workbook":      "PIC ULAE (2025) - Final.xlsx",
    "ulae_sheet":         "ULAE Calculation (Rev)",
}


@dataclass
class ClientConfig:
    client_id:           str
    name:                str
    data_folder:         Optional[str] = None   # Where this client's source Excel workbooks live (env var or client.yaml — see module docstring)
    results_folder:      Optional[str] = None   # Where generated reports/exports are written
    currency:            str = "GHS"
    fx_rate_ghs_usd:     float = 15.50
    data_files:           Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DATA_FILES))
    assumption_overrides:   Dict[str, Any] = field(default_factory=dict)
    reinsurance:              Dict[str, Any] = field(default_factory=dict)

    @property
    def root(self) -> str:
        return os.path.join(CLIENTS_ROOT, self.client_id)

    @property
    def products_dir(self) -> str:
        return os.path.join(self.root, "products")

    @property
    def assumptions_dir(self) -> str:
        return os.path.join(self.root, "assumptions")

    @property
    def data_dir_available(self) -> bool:
        """
        True only if data_folder is both configured AND actually reachable
        from this environment. On Railway, a client's data_folder env var
        may be unset (nothing configured) or the folder simply won't exist
        (real workbooks can't be uploaded to a cloud server) — either way,
        callers should check this before touching engine/data_loader.py so
        they can fail with a clear message instead of a confusing
        FileNotFoundError several layers down. See api/main.py's non-life
        routes.
        """
        return bool(self.data_folder) and os.path.isdir(self.data_folder)

    def data_file_path(self, key: str) -> str:
        """Full path to one of this client's non-life workbooks, e.g. data_file_path('ibnr_workbook')."""
        if not self.data_folder:
            raise ValueError(
                f"Client '{self.client_id}' has no data folder configured — set the "
                f"{self.client_id.upper()}_DATA_DIR environment variable, or data_folder "
                f"in clients/{self.client_id}/client.yaml for local development."
            )
        if key not in self.data_files:
            raise ValueError(f"Unknown data_files key '{key}' for client '{self.client_id}' — expected one of {sorted(self.data_files)}")
        return os.path.join(self.data_folder, self.data_files[key])


def list_clients() -> List[str]:
    """List every client_id with a clients/<id>/assumptions.yaml file (excludes the _template)."""
    if not os.path.isdir(CLIENTS_ROOT):
        return []
    ids = []
    for entry in sorted(os.listdir(CLIENTS_ROOT)):
        if entry.startswith("_"):
            continue
        if os.path.isfile(os.path.join(CLIENTS_ROOT, entry, "assumptions.yaml")):
            ids.append(entry)
    return ids


def load_client(client_id: str) -> ClientConfig:
    assumptions_path = os.path.join(CLIENTS_ROOT, client_id, "assumptions.yaml")
    if not os.path.isfile(assumptions_path):
        available = list_clients()
        raise ValueError(
            f"Unknown client '{client_id}' — no clients/{client_id}/assumptions.yaml found. "
            f"Available clients: {available or '(none configured)'}"
        )
    with open(assumptions_path, "r", encoding="utf-8") as f:
        raw: Dict[str, Any] = yaml.safe_load(f) or {}

    data_files = dict(DEFAULT_DATA_FILES)
    data_files.update(raw.get("data_files") or {})

    # client.yaml is optional and gitignored (local-dev only, real absolute
    # paths) — it may not exist at all in production. The env var always
    # takes priority when set, since that's the only mechanism Railway has.
    client_yaml_path = os.path.join(CLIENTS_ROOT, client_id, "client.yaml")
    client_raw: Dict[str, Any] = {}
    if os.path.isfile(client_yaml_path):
        with open(client_yaml_path, "r", encoding="utf-8") as f:
            client_raw = yaml.safe_load(f) or {}

    env_data_dir = os.environ.get(f"{client_id.upper()}_DATA_DIR")
    data_folder = env_data_dir or client_raw.get("data_folder")

    return ClientConfig(
        client_id             = client_id,
        name                  = raw.get("name", client_id),
        data_folder           = data_folder,
        results_folder        = client_raw.get("results_folder"),
        currency               = raw.get("currency", "GHS"),
        fx_rate_ghs_usd         = raw.get("fx_rate_ghs_usd", 15.50),
        data_files                = data_files,
        assumption_overrides       = raw.get("assumption_overrides") or {},
        reinsurance                  = raw.get("reinsurance") or {},
    )


def list_products(client_id: str) -> List[str]:
    """List every product name available for a client."""
    client = load_client(client_id)
    if not os.path.isdir(client.products_dir):
        return []
    return sorted(
        fname[:-5] for fname in os.listdir(client.products_dir)
        if fname.endswith(".yaml")
    )


def load_product(client_id: str, product_name: str) -> Product:
    client = load_client(client_id)
    path = os.path.join(client.products_dir, f"{product_name}.yaml")
    if not os.path.isfile(path):
        raise ValueError(f"Unknown product '{product_name}' for client '{client_id}' — no {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw.setdefault("name", product_name)
    return Product.from_dict(raw)
