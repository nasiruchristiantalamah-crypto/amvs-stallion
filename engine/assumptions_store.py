"""
================================================================================
ASSUMPTIONS STORE — versioned, per-client, per-product
================================================================================
What this file does:
    Loads and saves engine/assumptions.py's ProductAssumptions to/from
    clients/<client_id>/assumptions/<product_name>/*.yaml, with versioning:
    every save writes a NEW timestamped snapshot rather than overwriting
    the last one, so old assumptions can always be compared against new
    ones (diff_versions) or rolled back to (set_active_version).

    Layout:
        clients/<client_id>/assumptions/<product_name>/
            v1_2026-01-01T00-00-00.yaml    # a snapshot — never edited after creation
            v2_2026-03-15T09-12-04.yaml
            current.yaml                    # {"active": "v2_2026-03-15T09-12-04"}
================================================================================
"""

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import yaml

from engine.assumptions import ProductAssumptions
from engine.clients import load_client


@dataclass
class VersionInfo:
    version_id: str
    label:      str
    saved_at:   str


def _version_dir(client_id: str, product_name: str) -> str:
    client = load_client(client_id)
    return os.path.join(client.assumptions_dir, product_name)


def _pointer_path(client_id: str, product_name: str) -> str:
    return os.path.join(_version_dir(client_id, product_name), "current.yaml")


def list_versions(client_id: str, product_name: str) -> List[VersionInfo]:
    d = _version_dir(client_id, product_name)
    if not os.path.isdir(d):
        return []
    versions = []
    for fname in sorted(os.listdir(d)):
        if not fname.endswith(".yaml") or fname == "current.yaml":
            continue
        version_id = fname[:-5]
        with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        meta = raw.get("_meta", {})
        versions.append(VersionInfo(
            version_id = version_id,
            label      = meta.get("label", version_id),
            saved_at   = meta.get("saved_at", ""),
        ))
    versions.sort(key=lambda v: v.saved_at)
    return versions


def get_active_version(client_id: str, product_name: str) -> Optional[str]:
    pointer = _pointer_path(client_id, product_name)
    if not os.path.isfile(pointer):
        # No explicit pointer yet — default to the most recently saved version.
        versions = list_versions(client_id, product_name)
        return versions[-1].version_id if versions else None
    with open(pointer, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return raw.get("active")


def set_active_version(client_id: str, product_name: str, version: str) -> None:
    d = _version_dir(client_id, product_name)
    if not os.path.isfile(os.path.join(d, f"{version}.yaml")):
        raise ValueError(f"No such assumptions version '{version}' for {client_id}/{product_name}")
    with open(_pointer_path(client_id, product_name), "w", encoding="utf-8") as f:
        yaml.safe_dump({"active": version}, f)


def load_assumptions(client_id: str, product_name: str, version: str = "current") -> ProductAssumptions:
    """
    Load a client/product's assumptions. version="current" (default)
    resolves to whichever version the pointer file (or, absent that, the
    most recent snapshot) says is active.
    """
    resolved = version
    if version == "current":
        resolved = get_active_version(client_id, product_name)
        if resolved is None:
            raise ValueError(f"No assumptions saved yet for {client_id}/{product_name}")

    path = os.path.join(_version_dir(client_id, product_name), f"{resolved}.yaml")
    if not os.path.isfile(path):
        raise ValueError(f"No such assumptions version '{resolved}' for {client_id}/{product_name}")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw.pop("_meta", None)
    return ProductAssumptions.from_dict(raw)


def save_assumptions(
    client_id:    str,
    product_name: str,
    assumptions:  ProductAssumptions,
    label:        str = "",
    make_active:  bool = True,
) -> str:
    """
    Write a NEW timestamped snapshot — never overwrites a prior version.
    Returns the new version_id.
    """
    d = _version_dir(client_id, product_name)
    os.makedirs(d, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    existing = {v.version_id for v in list_versions(client_id, product_name)}
    n = len(existing) + 1
    version_id = f"v{n}_{timestamp}"
    while version_id in existing:  # extremely unlikely, but keep version ids unique
        n += 1
        version_id = f"v{n}_{timestamp}"

    payload = assumptions.to_dict()
    payload["_meta"] = {"label": label or version_id, "saved_at": datetime.now().isoformat()}

    with open(os.path.join(d, f"{version_id}.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False)

    if make_active:
        set_active_version(client_id, product_name, version_id)

    return version_id


def diff_versions(client_id: str, product_name: str, v1: str, v2: str) -> Dict[str, Tuple[Any, Any]]:
    """
    Field-by-field comparison of two assumption versions. Returns only the
    fields that differ: {field_name: (value_in_v1, value_in_v2)}.
    Nested structures (lapse_schedule, commission) are compared as whole
    dicts rather than recursing further, since that's the natural unit an
    actuary would review a change at.
    """
    a1 = load_assumptions(client_id, product_name, v1).to_dict()
    a2 = load_assumptions(client_id, product_name, v2).to_dict()

    diffs: Dict[str, Tuple[Any, Any]] = {}
    for key in sorted(set(a1.keys()) | set(a2.keys())):
        val1, val2 = a1.get(key), a2.get(key)
        if val1 != val2:
            diffs[key] = (val1, val2)
    return diffs
