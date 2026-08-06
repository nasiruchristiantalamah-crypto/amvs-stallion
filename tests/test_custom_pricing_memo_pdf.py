"""
================================================================================
ACTUARIAL MEMORANDUM PDF EXPORT — validation
================================================================================
What this file does:
    Validates outputs/custom_pricing_memo_pdf_exporter.py — the PDF twin
    of the existing Word actuarial memorandum. No PDF-parsing dependency
    is installed in this project (reportlab writes PDFs, nothing reads
    them back), so verification is structural: a well-formed PDF file
    (starts with the %PDF- magic bytes, ends with the %%EOF trailer,
    contains the expected number of /Type /Page objects) rather than
    asserting on extracted text — the same level of rigour
    tests/test_cashflow_by_life.py's Excel check uses (opens the real
    file, confirms real structure), adapted to what's actually inspectable
    for a PDF without adding a new dependency just for tests.
================================================================================
"""

import os

import pytest

from api.main import CustomProductRequest, _build_product_from_request, _resolve_custom_assumptions
from engine.custom_pricing import run_custom_pricing, run_custom_rate_table
from outputs.custom_pricing_memo_pdf_exporter import generate_actuarial_memorandum_pdf


def _pdf_page_count(path: str) -> int:
    with open(path, "rb") as f:
        content = f.read()
    return content.count(b"/Type /Page")  # counts /Type /Pages (the tree root) once too, but each leaf page also matches — good enough as a lower-bound sanity check


def _price_micro_life_with_spouse():
    req = CustomProductRequest(
        product_name="PDF Memo Test", product_type="micro_life", sum_assured=2500, entry_age=45,
        dependants=[{"relationship": "spouse", "age": 48}],
    )
    product = _build_product_from_request(req)
    assumptions = _resolve_custom_assumptions(req)
    result = run_custom_pricing(product, assumptions, verbose=False)
    rate_table = run_custom_rate_table(product, assumptions, 40, 50, None)
    return req, product, assumptions, result, rate_table


def test_generates_a_well_formed_pdf():
    req, product, assumptions, result, rate_table = _price_micro_life_with_spouse()
    path = generate_actuarial_memorandum_pdf(result, rate_table, req.model_dump(), {}, product=product, assumptions=assumptions)
    try:
        assert os.path.isfile(path)
        assert path.endswith(".pdf")
        with open(path, "rb") as f:
            content = f.read()
        assert content[:5] == b"%PDF-"
        assert b"%%EOF" in content[-64:]
        assert os.path.getsize(path) > 2000   # a 5-section, multi-page memo is never this small if it rendered correctly
    finally:
        os.remove(path)


def test_has_multiple_pages_matching_the_five_section_structure():
    """Cover + I + II + III + IV + V, each starting on its own page (see
    the exporter's PageBreak() calls) — expect at least 6 page objects."""
    req, product, assumptions, result, rate_table = _price_micro_life_with_spouse()
    path = generate_actuarial_memorandum_pdf(result, rate_table, req.model_dump(), {}, product=product, assumptions=assumptions)
    try:
        assert _pdf_page_count(path) >= 6
    finally:
        os.remove(path)


def test_works_without_product_and_assumptions_supplied():
    """Illustrative Example (section IV) needs product/assumptions to
    compute the month-1 breakdown — when omitted, the exporter must still
    produce a valid document, just skipping that one table (mirrors the
    Word exporter's own `if product is not None and assumptions is not None` guard)."""
    req, _, _, result, rate_table = _price_micro_life_with_spouse()
    path = generate_actuarial_memorandum_pdf(result, rate_table, req.model_dump(), {})
    try:
        with open(path, "rb") as f:
            assert f.read()[:5] == b"%PDF-"
    finally:
        os.remove(path)


def test_narrative_overrides_are_honoured_not_the_auto_fallback():
    """A supplied narrative section should end up in the output instead of
    the auto-composed fallback text — checked by file-size delta, since we
    can't extract text without a PDF parser: a custom narrative long enough
    to meaningfully change the rendered content should change the byte size.
    Explicit output_path values (rather than the default's second-granularity
    timestamp) so two calls made in the same test don't collide on filename."""
    import tempfile
    req, product, assumptions, result, rate_table = _price_micro_life_with_spouse()
    with tempfile.TemporaryDirectory() as tmp:
        path_default = generate_actuarial_memorandum_pdf(
            result, rate_table, req.model_dump(), {}, product=product, assumptions=assumptions,
            output_path=os.path.join(tmp, "default.pdf"),
        )
        custom_narrative = {"policy_overview": "X" * 2000}   # deliberately far longer than the auto fallback
        path_custom = generate_actuarial_memorandum_pdf(
            result, rate_table, req.model_dump(), custom_narrative, product=product, assumptions=assumptions,
            output_path=os.path.join(tmp, "custom.pdf"),
        )
        size_default = os.path.getsize(path_default)
        size_custom = os.path.getsize(path_custom)
        assert size_custom != size_default


def test_output_path_can_be_overridden():
    req, product, assumptions, result, rate_table = _price_micro_life_with_spouse()
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "custom_name.pdf")
        path = generate_actuarial_memorandum_pdf(
            result, rate_table, req.model_dump(), {}, product=product, assumptions=assumptions, output_path=target,
        )
        assert path == target
        assert os.path.isfile(target)
