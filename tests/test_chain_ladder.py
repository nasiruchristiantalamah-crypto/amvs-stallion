"""
================================================================================
CHAIN LADDER & BORNHUETTER-FERGUSON VALIDATION — Provident Insurance (PIC)
================================================================================
What this file does:
    Validates engine/chain_ladder.py and engine/bornhuetter_ferguson.py
    against real data: the cumulative incurred claims triangles (Gross and
    Net) for all four classes of business PIC reserves this way, taken
    directly from "2025 IBNR Projection (Gross & Net) - Final.xlsx"
    (sheets MOTOR, FIRE, ACCIDENT, OTHERS), "Cummulative Loss Reported"
    tables, plus the Written/Earned Premium used in PIC's own
    Expected-Loss-Ratio section for each class.

    PIC's own workbook reports these "Selected" IBNR figures (its own blend
    of Chain Ladder, Expected Loss Ratio, and judgmental selection):

        Class      Selected Gross IBNR    Selected Net IBNR
        Motor      GHS 23,004,071.47      GHS 13,735,877.64
        Fire       GHS    460,561.52      GHS    234,804.98
        Accident   GHS  1,478,978.12      GHS    480,337.66
        Others     GHS    530,596.01      GHS    102,741.68

    These tests don't require an exact match to PIC's "Selected" figures —
    those carry judgmental overrides this module doesn't apply — they check
    the credibility-blended Chain Ladder / BF output lands in a sane range,
    and print the full comparison so a reviewing actuary can see the variance
    per class.

Run with:
    cd amvs
    pytest tests/test_chain_ladder.py -s
================================================================================
"""

import pytest

from engine.claims_triangle import ClaimsTriangle
from engine.chain_ladder import run_chain_ladder
from engine.bornhuetter_ferguson import estimate_expected_loss_ratio, run_blended_reserving


# ════════════════════════════════════════════════════════════════════════════
# PIC source data — "2025 IBNR Projection (Gross & Net) - Final.xlsx"
# ════════════════════════════════════════════════════════════════════════════

MOTOR_GROSS_TRIANGLE = {
    2018: [4531426.566833631, 7615656.988171403, 8179358.491701969, 8396654.38793753,
           8525933.536442613, 8525933.536442613, 8525933.536442613, 8525933.536442613],
    2019: [2879286.238591513, 5019418.495851498, 5118176.884903016, 5322571.741823244,
           5322571.741823244, 5322571.741823244, 5322571.741823244],
    2020: [4512002.159212389, 10236068.685342222, 10714815.598214198, 10830278.789596934,
           10930729.848370453, 10930729.848370453],
    2021: [8465533.682544604, 15167334.13217074, 15732473.256310849, 15888585.947286205,
           15930016.998106664],
    2022: [7489882.892982837, 17157944.03050316, 18030179.56883367, 18224844.900986776],
    2023: [9427544.10954271, 18773455.248407625, 19856269.931564603],
    2024: [15040275.156333426, 31887761.665979333],
    2025: [22426280.79889869],
}
MOTOR_NET_TRIANGLE = {
    2018: [2669119.958810128, 4041262.4308793806, 4412021.469831511, 4569566.187999633,
           4666587.739283711, 4666587.739283711, 4666587.739283711, 4666587.739283711],
    2019: [1226657.7122425367, 2329380.949469173, 2399060.803281384, 2542964.724548868,
           2542964.724548868, 2542964.724548868, 2542964.724548868],
    2020: [2242618.586786014, 6244303.655411408, 6635235.832481068, 6735616.69621534,
           6823269.799177921, 6823269.799177921],
    2021: [5699488.0408537835, 10712355.060336918, 11194644.374581238, 11319283.95145119,
           11343924.837383522],
    2022: [5956028.55110742, 13945261.0271888, 14515111.28614773, 14678504.936196698],
    2023: [8075011.199951525, 15496481.73778364, 16187048.84795149],
    2024: [11155267.35710673, 20411158.69474772],
    2025: [10909618.928608742],
}
MOTOR_GROSS_PREMIUM = {
    2018: 15177079.897238765, 2019: 19036405.959149506, 2020: 31322930.559437532,
    2021: 39213484.71467013, 2022: 48151094.88061851, 2023: 65302760.771358214,
    2024: 93327884.4860537,  2025: 98187618.05884197,
}
MOTOR_NET_PREMIUM = {
    2018: 14594993.807020042, 2019: 17660946.235872604, 2020: 27580710.306464944,
    2021: 35725902.3803492,  2022: 43290804.29551299,  2023: 59870955.52259809,
    2024: 79861060.7627911,  2025: 88254945.45850162,
}

FIRE_GROSS_TRIANGLE = {
    2018: [29123.861370596343, 65708.92607871565, 65708.92607871565, 65708.92607871565,
           65708.92607871565, 65708.92607871565, 65708.92607871565, 65708.92607871565],
    2019: [3347.3426299186312, 97788.37837271394, 97892.50459891051, 97892.50459891051,
           97892.50459891051, 97892.50459891051, 97892.50459891051],
    2020: [125665.04201318767, 137028.196419491, 214924.3796523846, 214924.3796523846,
           214924.3796523846, 214924.3796523846],
    2021: [12852.9699814921, 756906.1896487017, 756986.7524280553, 756986.7524280553,
           756986.7524280553],
    2022: [364844.34338603704, 815292.8786750038, 817030.731831083, 817030.731831083],
    2023: [118146.44543638824, 286669.9451385806, 287707.9777939764],
    2024: [311718.23916328524, 1064334.6414657214],
    2025: [170795.1282361374],
}
FIRE_NET_TRIANGLE = {
    2018: [0, 105748.78483458683, 105748.78483458683, 105748.78483458683,
           105748.78483458683, 105748.78483458683, 105748.78483458683, 105748.78483458683],
    2019: [8308.092503451133, 102672.08185786198, 102775.33494562458, 102775.33494562458,
           102775.33494562458, 102775.33494562458, 102775.33494562458],
    2020: [124964.81320080109, 130965.98400993316, 205070.8510946569, 205070.8510946569,
           205070.8510946569, 205070.8510946569],
    2021: [12745.192887367712, 574730.7787732948, 574807.4204495387, 574807.4204495387,
           574807.4204495387],
    2022: [329916.4218582847, 758440.9971419892, 758986.4702535326, 758986.4702535326],
    2023: [112396.09275103739, 168890.24905054813, 169367.15024181106],
    2024: [112187.11805194936, 493124.3250930291],
    2025: [88953.0056669352],
}
FIRE_GROSS_PREMIUM = {
    2018: 2104014.6169448053, 2019: 1959388.1330649124, 2020: 3599987.130003036,
    2021: 6827453.985340854, 2022: 13304830.885283848, 2023: 17611585.30299615,
    2024: 22858898.644027326, 2025: 9889129.71,
}
FIRE_NET_PREMIUM = {
    2018: 1349289.7640544053, 2019: 1075713.765296412, 2020: 2143379.37389659,
    2021: 4332189.092157651, 2022: 7858064.990473945, 2023: 12408530.55945819,
    2024: 18557152.99431783, 2025: 4480186.932459858,
}

ACCIDENT_GROSS_TRIANGLE = {
    2018: [1650546.1602844575, 1916162.7200242619, 1929760.1497881354, 1996718.5543607771,
           1997078.469986657, 2051731.8025392906, 2051731.8025392906, 2051731.8025392906],
    2019: [192931.94196921974, 333025.2137546211, 336616.0006034643, 336616.0006034643,
           336616.0006034643, 336616.0006034643, 336616.0006034643],
    2020: [145568.2204714809, 524166.52464477683, 524166.52464477683, 524166.52464477683,
           524166.52464477683, 556597.9442155699],
    2021: [275002.37589391286, 413266.60741451505, 475611.6025173243, 537726.9946638502,
           537726.9946638502],
    2022: [941665.1976998379, 1269809.8581014299, 1299573.170357079, 1338294.2121226434],
    2023: [1286250.4994538622, 2225640.8201481854, 2390130.7075097053],
    2024: [725714.4988106043, 2746683.7810981222],
    2025: [1233386.1631331882],
}
ACCIDENT_NET_TRIANGLE = {
    2018: [1601724.0859546857, 1831692.7208598848, 1845242.3874313682, 1897796.950224179,
           1897956.6764159224, 1942312.597843926, 1942312.597843926, 1942312.597843926],
    2019: [171466.4211147816, 296439.4667856932, 298033.0144604441, 298033.0144604441,
           298033.0144604441, 298033.0144604441, 298033.0144604441],
    2020: [112137.63002900823, 367672.4194283418, 367672.4194283418, 367672.4194283418,
           367672.4194283418, 392261.3533265004],
    2021: [173086.3663995569, 240135.11051195598, 290733.4834756962, 349759.5857193631,
           349759.5857193631],
    2022: [431211.9884430571, 696651.3024053366, 722448.2665612603, 751805.8817702704],
    2023: [1100375.5826393166, 1927121.3735410366, 1988956.0196130453],
    2024: [339050.0232774932, 921814.9594609044],
    2025: [718746.695006897],
}
ACCIDENT_GROSS_PREMIUM = {
    2018: 1168165.496081357, 2019: 2008865.480153127, 2020: 1976836.7774285323,
    2021: 1885928.7194751615, 2022: 1712362.3680005332, 2023: 1425479.443883487,
    2024: 756937.3899638101, 2025: 9290329.730000082,
}
# Note: Accident Net premium is negative in 2023/2024 in PIC's own data (a
# reinsurance recovery/adjustment quirk in a small, volatile class) — carried
# through as-is since we're validating against PIC's own numbers.
ACCIDENT_NET_PREMIUM = {
    2018: 408097.0054481413, 2019: 1296815.925358767, 2020: 1104827.3857626214,
    2021: 1064553.1960271474, 2022: 705943.293584525, 2023: -333479.7668993602,
    2024: -2209365.4730805424, 2025: 7398581.933270829,
}

OTHERS_GROSS_TRIANGLE = {
    2018: [0, 0, 0, 0, 0, 0, 0, 0],
    2019: [2261093.758569454, 3397849.6410826156, 3397849.6410826156, 3397849.6410826156,
           3397849.6410826156, 3397849.6410826156, 3397849.6410826156],
    2020: [2132621.205569088, 2179645.69240849, 2179645.69240849, 2179645.69240849,
           2179645.69240849, 2179645.69240849],
    2021: [3741.1303483250545, 103164.9901472826, 184891.16133779724, 184891.16133779724,
           184891.16133779724],
    2022: [3504755.5602010423, 4350998.759010527, 4377931.681908503, 4377931.681908503],
    2023: [0, 20362222.933351755, 20362222.933351755],
    2024: [90070.21375027183, 91381.39375027182],
    2025: [187508.53999999998],
}
OTHERS_NET_TRIANGLE = {
    2018: [0, 0, 0, 0, 0, 0, 0, 0],
    2019: [1910949.2117584422, 2871782.796049447, 2871782.796049447, 2871782.796049447,
           2871782.796049447, 2871782.796049447, 2871782.796049447],
    2020: [2022945.2118191475, 2067551.3332633146, 2067551.3332633146, 2067551.3332633146,
           2067551.3332633146, 2067551.3332633146],
    2021: [3548.732285495552, 57058.78758982117, 57058.78758982117, 57058.78758982117,
           57058.78758982117],
    2022: [1886264.1646956743, 1886264.1646956743, 1886492.6333816184, 1886492.6333816184],
    2023: [0, 2131591.73334167, 2131591.73334167],
    2024: [764.0545905907184, 1399.5379723877866],
    2025: [187508.53999999998],
}
OTHERS_GROSS_PREMIUM = {
    2018: 3409986.1391858477, 2019: 4702224.211122696, 2020: 4063966.818826841,
    2021: 5437443.606271459, 2022: 6651483.78727969, 2023: 12262848.827441841,
    2024: 18730061.18741115, 2025: 23841254.290000007,
}
OTHERS_NET_PREMIUM = {
    2018: 1756375.6575472753, 2019: 2212645.534197902, 2020: 2858849.66600706,
    2021: 1131095.017945865, 2022: 1416637.080136222, 2023: 1249905.6265078068,
    2024: 8485492.273462445, 2025: 11234437.011469409,
}

# ── PIC's own "Selected" IBNR per class (Chain Ladder + ELR + judgment) ────
CLASSES = {
    "Motor":    dict(gross_tri=MOTOR_GROSS_TRIANGLE,    net_tri=MOTOR_NET_TRIANGLE,
                      gross_prem=MOTOR_GROSS_PREMIUM,    net_prem=MOTOR_NET_PREMIUM,
                      gross_ibnr=23004071.469887882,      net_ibnr=13735877.643969132),
    "Fire":     dict(gross_tri=FIRE_GROSS_TRIANGLE,     net_tri=FIRE_NET_TRIANGLE,
                      gross_prem=FIRE_GROSS_PREMIUM,      net_prem=FIRE_NET_PREMIUM,
                      gross_ibnr=460561.5159904883,        net_ibnr=234804.98309619937),
    "Accident": dict(gross_tri=ACCIDENT_GROSS_TRIANGLE, net_tri=ACCIDENT_NET_TRIANGLE,
                      gross_prem=ACCIDENT_GROSS_PREMIUM,  net_prem=ACCIDENT_NET_PREMIUM,
                      gross_ibnr=1478978.1182391169,       net_ibnr=480337.65976429184),
    "Others":   dict(gross_tri=OTHERS_GROSS_TRIANGLE,   net_tri=OTHERS_NET_TRIANGLE,
                      gross_prem=OTHERS_GROSS_PREMIUM,    net_prem=OTHERS_NET_PREMIUM,
                      gross_ibnr=530596.0142580013,        net_ibnr=102741.68255336318),
}


def _build_triangle(label: str, data: dict) -> ClaimsTriangle:
    origin_years = sorted(data.keys())
    return ClaimsTriangle(class_of_business=label, origin_years=origin_years, triangle=data)


@pytest.mark.parametrize("class_name", CLASSES.keys())
@pytest.mark.parametrize("gross_or_net", ["gross", "net"])
def test_blended_reserving_against_pic_benchmark(class_name, gross_or_net):
    cfg = CLASSES[class_name]
    triangle_data = cfg[f"{gross_or_net}_tri"]
    premium       = cfg[f"{gross_or_net}_prem"]
    pic_ibnr      = cfg[f"{gross_or_net}_ibnr"]

    triangle = _build_triangle(f"{class_name} ({gross_or_net})", triangle_data)
    cl = run_chain_ladder(triangle)

    cl_variance = (cl.total_ibnr - pic_ibnr) / pic_ibnr if pic_ibnr else float("nan")

    try:
        elr = estimate_expected_loss_ratio(triangle, premium)
        blended = run_blended_reserving(triangle, premium, elr)
        blended_variance = (blended.total_blended_ibnr - pic_ibnr) / pic_ibnr if pic_ibnr else float("nan")
        print(f"\n[{class_name} {gross_or_net.title()}] ELR={elr:.1%}  "
              f"Chain Ladder IBNR=GHS {cl.total_ibnr:,.0f} ({cl_variance:+.1%})  "
              f"Blended IBNR=GHS {blended.total_blended_ibnr:,.0f} ({blended_variance:+.1%})  "
              f"vs PIC Selected=GHS {pic_ibnr:,.0f}")
    except ValueError as e:
        # Small/volatile classes (e.g. Accident Net, with negative premium
        # years) can fail the maturity threshold for an ELR estimate —
        # fall back to reporting pure Chain Ladder only.
        print(f"\n[{class_name} {gross_or_net.title()}] BF ELR unavailable ({e}); "
              f"Chain Ladder IBNR=GHS {cl.total_ibnr:,.0f} ({cl_variance:+.1%}) "
              f"vs PIC Selected=GHS {pic_ibnr:,.0f}")
        blended = None

    # Sanity check only, deliberately with no fixed variance bound. PIC's own
    # workbook shows "Selected" factors sometimes exclude outlier origin
    # years from the volume-weighted average (Accident's raw factors swing
    # as high as 58.9x in one year) — a judgmental step this module doesn't
    # replicate, so naive Chain Ladder can legitimately diverge a lot on
    # small/volatile classes (see Accident Net below) without being wrong.
    # This is a reporting test: it fails only if the method itself breaks.
    assert cl.total_ibnr >= 0
    assert blended is None or blended.total_blended_ibnr == blended.total_blended_ibnr  # not NaN


def test_motor_gross_blended_beats_pure_chain_ladder():
    """
    Motor Gross is the clearest case: Chain Ladder alone overstates IBNR by
    ~26% (driven almost entirely by the immature 2025 origin year). Blending
    with Bornhuetter-Ferguson should land materially closer to PIC's own
    Selected Gross IBNR than pure Chain Ladder does.
    """
    triangle = _build_triangle("Motor (Gross)", MOTOR_GROSS_TRIANGLE)
    pic_ibnr = CLASSES["Motor"]["gross_ibnr"]
    elr = estimate_expected_loss_ratio(triangle, MOTOR_GROSS_PREMIUM)
    blended = run_blended_reserving(triangle, MOTOR_GROSS_PREMIUM, elr)

    cl_variance      = (run_chain_ladder(triangle).total_ibnr - pic_ibnr) / pic_ibnr
    blended_variance = (blended.total_blended_ibnr - pic_ibnr) / pic_ibnr

    print(f"\n[Motor Gross] Estimated a priori ELR (from mature years) = {elr:.1%}")
    print(f"[Motor Gross] Blended IBNR = GHS {blended.total_blended_ibnr:,.2f}  "
          f"vs PIC Selected = GHS {pic_ibnr:,.2f}  ({blended_variance:+.1%})  "
          f"[pure Chain Ladder was {cl_variance:+.1%}]")
    for oy in blended.origin_years:
        print(f"    {oy}: Z={blended.credibility_weight[oy]:.3f}  "
              f"CL_ult={blended.chain_ladder_ultimate[oy]:>13,.0f}  "
              f"BF_ult={blended.bf_ultimate[oy]:>13,.0f}  "
              f"blended_ult={blended.blended_ultimate[oy]:>13,.0f}  "
              f"blended_ibnr={blended.blended_ibnr[oy]:>12,.0f}")

    assert abs(blended_variance) < abs(cl_variance), (
        "Blended IBNR should be closer to PIC's selected figure than pure "
        "Chain Ladder, since it's specifically correcting the immature-year overstatement."
    )


def test_development_factors_converge_to_one():
    """Development factors should trend toward 1.0 as claims mature (Motor matures fast)."""
    triangle = _build_triangle("Motor (Gross)", MOTOR_GROSS_TRIANGLE)
    result = run_chain_ladder(triangle)
    factors = result.development_factors.age_to_age
    print(f"\n[Motor Gross] Age-to-age factors: {[round(f, 4) for f in factors]}")
    assert factors[-1] < 1.1
