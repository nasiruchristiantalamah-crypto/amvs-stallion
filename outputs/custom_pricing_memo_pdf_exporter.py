"""
================================================================================
CUSTOM PRODUCT PRICING — full Actuarial Memorandum (PDF)
================================================================================
What this file does:
    PDF twin of outputs/custom_pricing_memo_exporter.py's Word memorandum —
    same five sections (Policy Overview, Premium Estimation, Appendix rate
    table, Illustrative Example, Actuarial Declaration), same narrative/
    computed-content split, same data sources, built independently with
    reportlab rather than converting the .docx. Word and PDF are both
    "real" outputs of this memorandum, not a primary + a converted copy.

    Why reportlab and not a Word->PDF conversion: python-docx can't render
    to PDF itself, and a faithful DOCX->PDF conversion needs a rendering
    engine (LibreOffice headless, MS Word via COM) that isn't available on
    Railway's Python buildpack without a custom Dockerfile. reportlab is
    pure-Python and already an installed dependency (requirements.txt),
    so this works identically in local dev and production with no extra
    system packages.

    Reuses outputs/custom_pricing_memo_exporter.py's own
    _month_one_breakdown() and _auto_policy_overview() helpers directly —
    the Illustrative Example breakdown and the auto-composed overview
    fallback must never drift between the Word and PDF versions of the
    same memorandum.
================================================================================
"""

import os
from datetime import datetime
from typing import Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from engine.assumptions import ProductAssumptions
from engine.product import Product
from outputs.custom_pricing_memo_exporter import GENERATED_DIR, _auto_policy_overview, _month_one_breakdown

STALLION_NAVY = colors.HexColor("#1F3864")
LIGHT_GREY    = colors.HexColor("#F2F2F2")

_styles = getSampleStyleSheet()
_title_style = ParagraphStyle("MemoTitle", parent=_styles["Title"], textColor=STALLION_NAVY, alignment=TA_CENTER, fontSize=22)
_subtitle_style = ParagraphStyle("MemoSubtitle", parent=_styles["Heading2"], alignment=TA_CENTER, textColor=STALLION_NAVY)
_h1_style = ParagraphStyle("MemoH1", parent=_styles["Heading1"], textColor=STALLION_NAVY, spaceBefore=14, spaceAfter=8)
_h2_style = ParagraphStyle("MemoH2", parent=_styles["Heading2"], textColor=STALLION_NAVY, fontSize=12, spaceBefore=10, spaceAfter=6)
_body_style = ParagraphStyle("MemoBody", parent=_styles["Normal"], alignment=TA_JUSTIFY, spaceAfter=8, leading=14)
_italic_style = ParagraphStyle("MemoItalic", parent=_body_style, fontName="Helvetica-Oblique")
_centered_style = ParagraphStyle("MemoCentered", parent=_body_style, alignment=TA_CENTER)
_centered_italic_style = ParagraphStyle("MemoCenteredItalic", parent=_centered_style, fontName="Helvetica-Oblique")

_TABLE_STYLE = TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), STALLION_NAVY),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 9),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GREY]),
    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ("TOPPADDING", (0, 0), (-1, -1), 4),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
])


def _fmt_pct(x) -> str:
    try:
        return f"{float(x) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(x) if x is not None else "—"


def _fmt_money(x) -> str:
    try:
        return f"GHS {float(x):,.2f}"
    except (TypeError, ValueError):
        return str(x) if x is not None else "—"


def _kv_table(rows: List[tuple], col_widths=(2.8 * inch, 3.2 * inch)) -> Table:
    data = [[str(k), str(v)] for k, v in rows]
    t = Table(data, colWidths=list(col_widths))
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHT_GREY]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def generate_actuarial_memorandum_pdf(
    result:             dict,
    rate_table:         Dict[int, dict],
    product_spec:       dict,
    narrative:          Optional[dict] = None,
    appointed_actuary:  str = "Charles Osei-Akoto, ASA, MAAA",
    consulting_firm:    str = "Stallion Consultants Ltd",
    report_date:        Optional[str] = None,
    product:            Optional[Product] = None,
    assumptions:        Optional[ProductAssumptions] = None,
    output_path:        Optional[str] = None,
) -> str:
    """
    Build a full actuarial pricing memorandum as a PDF and write it to disk.
    Same parameters, same five sections, same content as
    custom_pricing_memo_exporter.generate_actuarial_memorandum() — see
    that function's docstring for what each parameter means.

    Returns:
        The path the PDF was written to.
    """
    narrative = narrative or {}
    report_date = report_date or datetime.now().strftime("%d %B %Y")
    a = result.get("assumptions_used", {})
    product_name = result.get("product_name") or product_spec.get("product_name", "Custom Product")

    if output_path is None:
        os.makedirs(GENERATED_DIR, exist_ok=True)
        name_slug = "".join(c if c.isalnum() else "_" for c in product_name)[:40]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(GENERATED_DIR, f"ActuarialMemo_{name_slug}_{timestamp}.pdf")

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch, topMargin=0.9 * inch, bottomMargin=0.9 * inch,
        title=f"Actuarial Memorandum — {product_name}", author=consulting_firm,
    )
    story = []

    # ── Cover page ──────────────────────────────────────────────────────
    story.append(Spacer(1, 2 * inch))
    story.append(Paragraph("ACTUARIAL MEMORANDUM", _title_style))
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph(f"FOR {product_name.upper()}", _subtitle_style))
    story.append(Spacer(1, 0.4 * inch))
    story.append(Paragraph(report_date, _centered_italic_style))
    story.append(Spacer(1, 1.2 * inch))
    story.append(Paragraph(f"<b>Presented By:</b><br/>{consulting_firm}", _centered_style))
    story.append(PageBreak())

    # ── I. Policy Overview ───────────────────────────────────────────────
    story.append(Paragraph("I. Policy Overview", _h1_style))
    story.append(Paragraph(narrative.get("policy_overview") or _auto_policy_overview(product_spec), _body_style))

    story.append(Paragraph("A. Number of Insureds", _h2_style))
    dependants = product_spec.get("dependants") or []
    story.append(Paragraph(
        f"This policy covers the Main Life{' as well as ' + str(len(dependants)) + ' additional insured life(ves)' if dependants else ' only'}.",
        _body_style,
    ))

    story.append(Paragraph("B. Policy Benefits", _h2_style))
    benefit_rows = [["Benefit", "Basis", "Amount (GHS)"],
                     ["Main Benefit (Sum Assured)", product_spec.get("product_type", ""), str(product_spec.get("sum_assured", 0))]]
    for r in (product_spec.get("riders") or []):
        benefit_rows.append([str(r.get("name", "")), str(r.get("benefit_type", "")), str(r.get("benefit_amount", ""))])
    t = Table(benefit_rows, colWidths=[2.6 * inch, 1.8 * inch, 1.6 * inch])
    t.setStyle(_TABLE_STYLE)
    story.append(t)
    story.append(Spacer(1, 10))

    story.append(Paragraph("C-E. Policy Proceeds, Surrender &amp; Settlement", _h2_style))
    story.append(Paragraph(narrative.get("proceeds_and_settlement") or
        "Proceeds are payable as a lump sum upon the occurrence of an insured event, equal to the "
        "applicable benefit amount(s) less any outstanding premium required to maintain cover to the date "
        "of the event.", _body_style))

    story.append(Paragraph("F. Premium", _h2_style))
    story.append(Paragraph(narrative.get("premium_notes") or
        f"Premiums are payable {product_spec.get('premium_mode', 'monthly')}. The premium covers the risk "
        f"cost of the benefits under this policy, together with policy fees, expenses, commission, and "
        f"the target profit margin.", _body_style))

    story.append(Paragraph("G. Underwriting Conditions", _h2_style))
    story.append(Paragraph(narrative.get("underwriting_notes") or
        f"Entry age: {product_spec.get('entry_age', '—')}. Gender basis: {product_spec.get('gender', 'unisex')}. "
        f"Policy term: {product_spec.get('policy_term_years') or 'Whole of life'}.", _body_style))

    story.append(Paragraph("H-I. Lapsation &amp; Reinstatement", _h2_style))
    story.append(Paragraph(narrative.get("lapsation_and_reinstatement") or
        "If premiums are not paid, the policy is subject to a grace period before lapsing. A lapsed policy "
        "may be eligible for reinstatement subject to the insurer's terms and payment of arrears.", _body_style))

    story.append(PageBreak())

    # ── II. Premium Estimation ───────────────────────────────────────────
    story.append(Paragraph("II. Premium Estimation", _h1_style))
    story.append(Paragraph(
        "The premium was determined using the equivalence principle: expected cash flows "
        "(premiums, benefits, commission, expenses) were projected and discounted, and the "
        "premium solved for that achieves the target profit margin.", _body_style,
    ))

    story.append(Paragraph("A. Insurance Risk Assumptions", _h2_style))
    story.append(_kv_table([
        ("Mortality basis", a.get("mortality_table", "SA 85/90 (Actuarial Society of South Africa)")),
        ("Mortality loading", _fmt_pct(a.get("mortality_loading"))),
        ("Gender basis", a.get("gender_main_str")),
        ("Valuation interest rate (p.a.)", _fmt_pct(a.get("valuation_rate_pa"))),
        ("Investment rate of return (p.a.)", _fmt_pct(a.get("investment_rate_pa"))),
        ("Expense inflation rate (p.a.)", _fmt_pct(a.get("expense_inflation_pa"))),
        ("Collection rate", _fmt_pct(a.get("collection_rate"))),
        ("Acquisition cost (GHS)", a.get("acquisition_cost")),
        ("Renewal expense (GHS p.a.)", a.get("renewal_expense_annual")),
        ("Policy fee (GHS/month)", a.get("policy_fee_monthly")),
        ("Target profit margin", _fmt_pct(a.get("target_profit_margin"))),
    ]))
    story.append(Spacer(1, 10))

    commission = a.get("commission") or {}
    story.append(_kv_table([
        ("Commission — initial year", _fmt_pct(commission.get("initial_rate"))),
        ("Commission — renewal years", _fmt_pct(commission.get("renewal_rate"))),
    ]))

    lapse = (a.get("lapse_schedule") or {}).get("rates", {})
    if lapse:
        story.append(Spacer(1, 10))
        story.append(Paragraph("Lapse rates:", _body_style))
        lapse_rows = [["Policy year", "Lapse rate"]] + [
            [str(yr), _fmt_pct(lapse[yr])] for yr in sorted(lapse.keys(), key=lambda x: int(x))
        ]
        t = Table(lapse_rows, colWidths=[2.5 * inch, 2.5 * inch])
        t.setStyle(_TABLE_STYLE)
        story.append(t)

    story.append(PageBreak())

    # ── III. Appendix: Monthly Risk Premium ──────────────────────────────
    story.append(Paragraph("III. Appendix: Monthly Risk Premium", _h1_style))
    story.append(Paragraph("The monthly risk premium by issue age of the main life is shown below.", _body_style))
    rate_rows = [["Age", "Monthly Premium (GHS)", "Onerous?"]]
    for age in sorted(rate_table.keys()):
        entry = rate_table[age]
        rate_rows.append([
            str(age), str(entry.get("error") or entry.get("monthly_premium")),
            "Yes" if entry.get("is_onerous") else ("—" if entry.get("error") else "No"),
        ])
    t = Table(rate_rows, colWidths=[1.5 * inch, 2.5 * inch, 2 * inch], repeatRows=1)
    t.setStyle(_TABLE_STYLE)
    story.append(t)

    story.append(PageBreak())

    # ── IV. Illustrative Example ──────────────────────────────────────────
    story.append(Paragraph("IV. Illustrative Example", _h1_style))
    if product is not None and assumptions is not None:
        breakdown = _month_one_breakdown(product, assumptions, result.get("monthly_premium", 0))
        story.append(Paragraph(
            f"Breakdown of the monthly premium of {_fmt_money(result.get('monthly_premium'))} "
            f"at entry age {product_spec.get('entry_age')}:", _body_style,
        ))
        breakdown_rows = [
            (label, _fmt_money(breakdown[key])) for label, key in [
                ("Pure Risk Premium", "pure_risk_premium"), ("Policy fee", "policy_fee"),
                ("Expenses", "expenses"), ("Commission", "commission"),
                ("Profit Margin", "profit_margin"), ("Monthly Premium (Total)", "total"),
            ]
        ]
        story.append(_kv_table(breakdown_rows))
        story.append(Spacer(1, 10))

    story.append(Paragraph("Additional Notes", _h2_style))
    story.append(Paragraph(narrative.get("closing_notes") or
        "Actual experience (mortality, morbidity, lapse, expenses, and investment returns) may differ "
        "from the assumptions above; such variances will emerge as additional profit or loss over the "
        "life of the policy.", _body_style))

    story.append(PageBreak())

    # ── V. Actuarial Declaration ──────────────────────────────────────────
    story.append(Paragraph("V. Actuarial Declaration", _h1_style))
    story.append(Paragraph(
        "This is to certify that the methodology used in pricing this policy is in accordance with "
        "internationally accepted actuarial principles.", _body_style,
    ))
    story.append(Spacer(1, 0.6 * inch))
    story.append(Paragraph("_" * 30 + f"&nbsp;&nbsp;&nbsp;&nbsp;{report_date}", _body_style))
    story.append(Paragraph(f"<b>{appointed_actuary}</b>", _body_style))
    story.append(Paragraph("(Consulting Actuary)", _italic_style))
    story.append(Paragraph(consulting_firm, _body_style))

    doc.build(story)
    return output_path
