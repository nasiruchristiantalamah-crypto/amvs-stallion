"""
================================================================================
ROLL-FORWARD SNAPSHOT STORE
================================================================================
What this file does:
    Persists each reporting period's closing IFRS 17 balance
    (engine/ifrs17.py's "closing_snapshot") so the NEXT period's
    generate_ifrs17_report() call can chain its opening balance to it —
    real period N+1 opening = real period N closing, not a fresh
    from-scratch re-measurement.

    Stored at clients/<client_id>/results/<product_name>_rollforward.json,
    one file per client+product, keyed by period_index.
================================================================================
"""

import json
import os
from typing import Dict, Optional

from engine.clients import load_client


def _snapshot_path(client_id: str, product_name: str) -> str:
    client = load_client(client_id)
    results_dir = client.results_folder or os.path.join(client.root, "results")
    os.makedirs(results_dir, exist_ok=True)
    return os.path.join(results_dir, f"{product_name}_rollforward.json")


def load_prior_closing(client_id: str, product_name: str, period_index: int) -> Optional[Dict[str, float]]:
    """Load period (period_index - 1)'s closing snapshot, if one was saved."""
    if period_index <= 1:
        return None
    path = _snapshot_path(client_id, product_name)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get(str(period_index - 1))


def save_closing_snapshot(client_id: str, product_name: str, period_index: int, closing: Dict[str, float]) -> None:
    path = _snapshot_path(client_id, product_name)
    data = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    data[str(period_index)] = closing
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def clear_snapshots(client_id: str, product_name: str) -> None:
    """Wipe all saved roll-forward history for a client/product (e.g. after
    an assumption change that invalidates the chained roll-forward)."""
    path = _snapshot_path(client_id, product_name)
    if os.path.isfile(path):
        os.remove(path)
