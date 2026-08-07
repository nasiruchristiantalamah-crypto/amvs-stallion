"""
================================================================================
UPLOAD FILE DETECTION — validation
================================================================================
What this file does:
    Validates api.main._detect_uploaded_file_kind() and
    _identify_and_rename_uploaded_files() — the content-based (sheet-name)
    identification that replaced exact-filename matching for the "upload
    your own client data" flow. Uses copies of PIC's real workbooks,
    renamed to deliberately generic, non-PIC, non-2025 filenames, proving
    detection genuinely doesn't depend on the year or company name baked
    into a filename the way the old exact-match check did.

    Skips gracefully (rather than failing) if PIC's real local data folder
    isn't reachable on this machine — same convention as the other
    real-PIC-data tests in this suite.
================================================================================
"""

import os
import shutil

import pytest

from engine.clients import load_client

try:
    _PIC_DATA_FOLDER = load_client("pic").data_folder
    _PIC_DATA_AVAILABLE = bool(_PIC_DATA_FOLDER and os.path.isdir(_PIC_DATA_FOLDER))
except Exception:
    _PIC_DATA_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _PIC_DATA_AVAILABLE, reason="PIC's real local data folder isn't reachable on this machine")


@pytest.fixture(scope="module")
def renamed_upload_dir(tmp_path_factory):
    """Copies of PIC's real 4 workbooks under deliberately generic,
    non-PIC, non-2025 filenames — proves detection is content-based."""
    from api.main import UPLOAD_TEMPLATE_CLIENT_ID

    template = load_client(UPLOAD_TEMPLATE_CLIENT_ID)
    dest_dir = tmp_path_factory.mktemp("renamed_upload")

    rename_map = {
        template.data_files["ibnr_workbook"]:     "my_claims_triangles_2099.xlsx",
        template.data_files["raw_data_workbook"]:  "AcmeCorp_case_reserves.xlsx",
        template.data_files["upr_dac_workbook"]:   "unearned_premium_workbook.xlsx",
        template.data_files["ulae_workbook"]:      "expense_loading_figures.xlsx",
    }
    for original_name, generic_name in rename_map.items():
        shutil.copyfile(
            os.path.join(template.data_folder, original_name),
            os.path.join(str(dest_dir), generic_name),
        )
    return str(dest_dir), rename_map


def test_each_generically_named_file_is_correctly_identified(renamed_upload_dir):
    from api.main import _detect_uploaded_file_kind

    dest_dir, rename_map = renamed_upload_dir
    expected_kind_by_generic_name = {
        "my_claims_triangles_2099.xlsx":     "ibnr_workbook",
        "AcmeCorp_case_reserves.xlsx":         "raw_data_workbook",
        "unearned_premium_workbook.xlsx":        "upr_dac_workbook",
        "expense_loading_figures.xlsx":            "ulae_workbook",
    }
    for generic_name, expected_kind in expected_kind_by_generic_name.items():
        path = os.path.join(dest_dir, generic_name)
        assert _detect_uploaded_file_kind(path) == expected_kind, generic_name


def test_a_file_matching_none_of_the_four_kinds_returns_none(tmp_path):
    import openpyxl
    from api.main import _detect_uploaded_file_kind

    path = os.path.join(str(tmp_path), "unrelated.xlsx")
    wb = openpyxl.Workbook()
    wb.active.title = "Random Sheet"
    wb.save(path)
    assert _detect_uploaded_file_kind(path) is None


def test_identify_and_rename_makes_the_files_loadable_by_the_standard_pipeline(renamed_upload_dir):
    """The real end-to-end proof: after renaming, engine.data_loader's
    normal (filename-trusting) loaders must be able to read the temp dir
    directly — confirms _identify_and_rename_uploaded_files() bridges
    content-detection back to the existing exact-match loading code
    without needing to change that code at all."""
    from api.main import _identify_and_rename_uploaded_files
    from engine.ifrs17_nonlife import generate_nonlife_paa_statements

    dest_dir, _ = renamed_upload_dir
    _identify_and_rename_uploaded_files(dest_dir)

    statements = generate_nonlife_paa_statements(client_id="pic", data_folder_override=dest_dir, verbose=False)
    assert statements["classes"]
    assert statements["totals"]["gross"].total_liability != 0


def test_missing_a_required_kind_raises_a_keyword_based_error_not_a_filename_one(tmp_path):
    from fastapi import HTTPException
    from api.main import _identify_and_rename_uploaded_files

    # Only 3 of 4 required files present.
    from api.main import UPLOAD_TEMPLATE_CLIENT_ID
    template = load_client(UPLOAD_TEMPLATE_CLIENT_ID)
    for key in ("ibnr_workbook", "raw_data_workbook", "upr_dac_workbook"):
        shutil.copyfile(
            os.path.join(template.data_folder, template.data_files[key]),
            os.path.join(str(tmp_path), f"file_{key}.xlsx"),
        )

    with pytest.raises(HTTPException) as exc_info:
        _identify_and_rename_uploaded_files(str(tmp_path))
    detail = str(exc_info.value.detail)
    assert "ULAE" in detail          # describes what's missing by KIND...
    assert "2025" not in detail      # ...not by PIC's literal filename
    assert "PIC" not in detail
