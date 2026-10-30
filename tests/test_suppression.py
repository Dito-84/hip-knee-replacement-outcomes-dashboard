"""Small-cell suppression: counts of 1 to 10, and anything computed from them, are hidden."""


from helpers import complication_stay, make_records, stay
from outcomes.load import load
from outcomes.metrics import build_dashboard_data
from outcomes.suppression import suppress


def _suppression_case(tmp_path):
    # 15 surgeries at one hospital: 3 with an infection, none with a death.
    rows = ([complication_stay(dob=f"01/{d:02d}/1950") for d in range(1, 4)]
            + [stay(dob=f"02/{d:02d}/1950") for d in range(1, 13)])
    path = tmp_path / "case.csv"
    make_records(rows).to_csv(path, index=False)
    return build_dashboard_data(load(path), "test")


def test_suppression_hides_counts_of_1_to_10_and_what_is_computed_from_them(tmp_path):
    data = _suppression_case(tmp_path)
    hidden = suppress(data, 11)
    k = hidden["views"]["All|All|all"]["kpis"]
    assert k["stays"] == 15                                     # 11 or more: shown
    assert k["complications"] is None and k["complication_rate"] is None
    assert k["deaths"] == 0 and k["mortality_rate"] == 0.0      # zero is shown
    types = {r["type"]: r for r in hidden["views"]["All|All|all"]["rate_by_type"]}
    assert types["Infection"]["stays"] is None and types["Infection"]["ci"] == [None, None]
    assert types["AMI"]["stays"] == 0
    row = hidden["views"]["__hospitals__|All|all"][0]
    assert row["hospital"] == "Hospital A"
    assert row["complications"] is None and row["oe_ratio"] is None and row["complication_ci"] == [None, None]
    assert hidden["suppressed_under"] == 11
    assert data["views"]["All|All|all"]["kpis"]["complications"] == 3     # the original is untouched


def test_build_suppress_flag(tmp_path):
    import build
    path = tmp_path / "case.csv"
    make_records([complication_stay(dob=f"01/{d:02d}/1950") for d in range(1, 4)] +
                 [stay(dob=f"02/{d:02d}/1950") for d in range(1, 13)]).to_csv(path, index=False)
    build.main(["--data", str(path), "--out", str(tmp_path / "site"), "--suppress-under", "11"])
    assert '"suppressed_under":11' in (tmp_path / "site" / "index.html").read_text()
