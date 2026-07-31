"""
================================================================================
CLIENT REGISTRY
================================================================================
What this file does:
    AMVS is no longer hardcoded to one insurer. Every client (insurer) has
    its own folder under clients/<client_id>/ with:
        client.yaml           — name, data folder, results folder, currency,
                                 non-life workbook filenames (data_files —
                                 different clients' analysts name and lay
                                 out their workbooks differently, even when
                                 the underlying sheets are the same shape;
                                 see engine/data_loader.py), assumption
                                 overrides, and reinsurance structure
        products/<name>.yaml   — product definitions (engine/product.py's Product)
        assumptions/<product>/ — versioned pricing/valuation assumptions
                                 (engine/assumptions_store.py)

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
# that differ in their client.yaml's data_files: section.
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
    data_folder:         Optional[str] = None   # Where this client's source Excel workbooks live
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

    def data_file_path(self, key: str) -> str:
        """Full path to one of this client's non-life workbooks, e.g. data_file_path('ibnr_workbook')."""
        if not self.data_folder:
            raise ValueError(f"Client '{self.client_id}' has no data_folder configured in client.yaml")
        if key not in self.data_files:
            raise ValueError(f"Unknown data_files key '{key}' for client '{self.client_id}' — expected one of {sorted(self.data_files)}")
        return os.path.join(self.data_folder, self.data_files[key])


def list_clients() -> List[str]:
    """List every client_id with a clients/<id>/client.yaml file (excludes the _template)."""
    if not os.path.isdir(CLIENTS_ROOT):
        return []
    ids = []
    for entry in sorted(os.listdir(CLIENTS_ROOT)):
        if entry.startswith("_"):
            continue
        if os.path.isfile(os.path.join(CLIENTS_ROOT, entry, "client.yaml")):
            ids.append(entry)
    return ids


def load_client(client_id: str) -> ClientConfig:
    path = os.path.join(CLIENTS_ROOT, client_id, "client.yaml")
    if not os.path.isfile(path):
        available = list_clients()
        raise ValueError(
            f"Unknown client '{client_id}' — no clients/{client_id}/client.yaml found. "
            f"Available clients: {available or '(none configured)'}"
        )
    with open(path, "r", encoding="utf-8") as f:
        raw: Dict[str, Any] = yaml.safe_load(f) or {}

    data_files = dict(DEFAULT_DATA_FILES)
    data_files.update(raw.get("data_files") or {})

    return ClientConfig(
        client_id             = client_id,
        name                  = raw.get("name", client_id),
        data_folder           = raw.get("data_folder"),
        results_folder        = raw.get("results_folder"),
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
