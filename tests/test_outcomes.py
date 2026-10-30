from pathlib import Path

import pandas as pd
import pytest

from outcomes import synthetic
from outcomes.load import DataFormatError, load
from outcomes.metrics import (build_dashboard_data, build_stays, hip_vs_knee_odds, hospital_table, mantel_haenszel,
                              odds_ratio, poisson_ratio_interval, summarize, wilson_interval)
from outcomes.suppression import suppress
from outcomes.schema import COLUMNS, match_headers

ROOT = Path(__file__).resolve().parent.parent
SRC = {name: src for name, (src, _) in COLUMNS.items()}


def make_records(rows):
    """Build a small source-format table: each row is a dict of internal names."""
    df = pd.DataFrame(rows).reindex(columns=list(COLUMNS)).fillna("")
    return df.rename(columns=SRC)


def stay(hospital="Hospital A", dob="01/01/1950", admit="03/01/2020", discharge="03/03/2020",
         tka=1, tha=0, excluded="No", **extra):
    row = dict(hospital=hospital, beneficiary_dob=dob, admission_date=admit, discharge_date=discharge,
               n_tka=tka, n_tha=tha, excluded=excluded, had_complication="No", complication="",
               additional_record="No", covid_excluded="No")
    row.update(extra)
    return row


@pytest.fixture
def small(tmp_path):
    rows = [
        stay(),                                                   # clean TKA
        stay(dob="02/02/1945", tka=0, tha=1),                     # clean THA
        # One patient, two complication records: must count as ONE stay
        stay(dob="03/03/1940", had_complication="Yes", complication="Infection",
             during_index_stay="No", complication_admit_date="03/13/2020"),
        stay(dob="03/03/1940", had_complication="Yes", complication="Mechanical complication",
             additional_record="Yes", during_index_stay="No", complication_admit_date="03/13/2020"),
        # Excluded stay with a COVID-flagged pneumonia readmitted at day 40
        stay(dob="04/04/1955", excluded="Yes", had_complication="Yes", complication="Pneumonia",
             covid_excluded="Yes", during_index_stay="No", complication_admit_date="04/12/2020"),
    ]
    path = tmp_path / "small.csv"
    make_records(rows).to_csv(path, index=False)
    return load(path)


# ---------- headers ----------

def test_headers_match_with_line_breaks_and_hints():
    messy = ["Hospital", "Beneficiary DOB", "Admission Date of Index Stay", "Discharge Date of Index Stay",
             "Number of TKAs Performed \n(0, 1, or 2)", "Number of THAs Performed \n(0, 1, or 2)",
             "Patient Had a Complication (Yes/No)", "Excluded (Yes/No)", "Complication",
             "Inclusion/\nExclusion Indicator", "Complication Excluded Due to COVID-19 (Yes/No)"]
    mapping, missing = match_headers(messy)
    assert missing == []
    assert mapping["Inclusion/\nExclusion Indicator"] == "inclusion_exclusion"
    assert mapping["Complication"] == "complication"
    assert mapping["Complication Excluded Due to COVID-19 (Yes/No)"] == "covid_excluded"
    assert mapping["Excluded (Yes/No)"] == "excluded"


def test_missing_required_column_is_reported():
    _, missing = match_headers(["Hospital", "Measure"])
    assert "admission_date" in missing


def test_excel_with_title_rows_above_headers(tmp_path):
    records = make_records([stay(), stay(dob="05/05/1951")])
    path = tmp_path / "export.xlsx"
    with pd.ExcelWriter(path) as xl:
        pd.DataFrame([["Report title"], [""]]).to_excel(xl, sheet_name="Notes", index=False, header=False)
        frame = pd.concat([pd.DataFrame([["Discharge-level report"] + [""] * (records.shape[1] - 1)],
                                        columns=records.columns), records.columns.to_frame().T, records])
        frame.to_excel(xl, sheet_name="Data", index=False, header=False)
    df = load(path)
    assert len(df) == 2
    assert df["admission_date"].notna().all()


def test_unrecognized_file_raises(tmp_path):
    path = tmp_path / "other.csv"
    pd.DataFrame({"a": [1]}).to_csv(path, index=False)
    with pytest.raises(DataFormatError):
        load(path)


# ---------- stay-level counting ----------

def test_multiple_complication_records_count_as_one_stay(small):
    stays, comps = build_stays(small)
    assert len(stays) == 4
    assert len(comps) == 3
    k = summarize(stays, comps)["kpis"]
    assert k["stays"] == 4
    assert k["complications"] == 2          # stays with a complication, not records
    assert k["complication_rate"] == pytest.approx(0.5)


def test_complication_types_count_records(small):
    stays, comps = build_stays(small)
    by_type = dict(zip(*summarize(stays, comps)["by_type"].values()))
    assert by_type["Infection"] == 1
    assert by_type["Mechanical complication"] == 1
    assert by_type["Pneumonia"] == 1


def test_thirty_day_readmission_window(small):
    stays, comps = build_stays(small)
    k = summarize(stays, comps)["kpis"]
    assert k["readmit30"] == 1              # day 10 counts, day 40 does not


def test_procedure_filter(small):
    stays, comps = build_stays(small)
    assert summarize(stays, comps, procedure="THA")["kpis"]["stays"] == 1
    assert summarize(stays, comps, procedure="TKA")["kpis"]["stays"] == 3


def test_adjusted_population_drops_excluded_stays(small):
    stays, comps = build_stays(small)
    k = summarize(stays, comps, population="adjusted")["kpis"]
    assert k["stays"] == 3
    assert k["complications"] == 1


def test_length_of_stay_and_age(small):
    stays, _ = build_stays(small)
    assert (stays["los_days"] == 2).all()
    first = stays.set_index("beneficiary_dob").loc[pd.Timestamp("1950-01-01")]
    assert first["age"] == 70


def test_wilson_interval_contains_rate():
    low, high = wilson_interval(3, 100)
    assert 0 < low < 0.03 < high < 0.1
    assert wilson_interval(0, 0) == (None, None)


# ---------- synthetic data ----------

def test_synthetic_is_reproducible():
    a = synthetic.generate(n_stays=200)
    b = synthetic.generate(n_stays=200)
    pd.testing.assert_frame_equal(a, b)


def test_synthetic_matches_source_format(tmp_path):
    path = tmp_path / "synthetic.csv"
    synthetic.generate(n_stays=300).to_csv(path, index=False)
    df = load(path)
    stays, _ = build_stays(df)
    assert len(stays) == 300
    rate = stays["complication_all"].mean()
    assert 0 < rate < 0.15


def test_committed_synthetic_file_is_current():
    committed = pd.read_csv(ROOT / "data" / "synthetic_discharges.csv", dtype=str, keep_default_na=False)
    fresh = synthetic.generate().astype(str)
    pd.testing.assert_frame_equal(committed.reset_index(drop=True), fresh.reset_index(drop=True))


def test_dashboard_data_has_every_view():
    data = build_dashboard_data(load(ROOT / "data" / "synthetic_discharges.csv"), "synthetic")
    hospitals = data["hospitals"]
    assert hospitals[0] == "All"
    expected = len(hospitals) * 3 * 2 + 3 * 2
    assert len(data["views"]) == expected


# ---------- errors found in the original Qlik formulas ----------
# Each case is made up, with an answer known by hand. They pin down that the
# rebuild doesn't repeat a mistake in the original measures (docs/measure-crosswalk.md).

def _load_rows(tmp_path, rows):
    path = tmp_path / "case.csv"
    make_records(rows).to_csv(path, index=False)
    return build_stays(load(path))


def test_knee_rates_use_knee_surgeries_as_denominator(tmp_path):
    # One knee surgery with an AMI, three clean hip surgeries.
    # The original knee AMI rate divided by hip admissions (1/3); the right answer is 1/1.
    stays, comps = _load_rows(tmp_path, [
        stay(dob="01/01/1950", had_complication="Yes", complication="AMI", during_index_stay="Yes"),
        stay(dob="02/02/1950", tka=0, tha=1),
        stay(dob="03/03/1950", tka=0, tha=1),
        stay(dob="04/04/1950", tka=0, tha=1),
    ])
    tka = summarize(stays, comps, procedure="TKA")
    assert tka["kpis"]["stays"] == 1
    assert tka["kpis"]["complication_rate"] == pytest.approx(1.0)
    assert dict(zip(*tka["by_type"].values()))["AMI"] == 1
    assert summarize(stays, comps, procedure="THA")["kpis"]["complication_rate"] == pytest.approx(0.0)


def test_adjusted_mortality_drops_excluded_stays_from_numerator(tmp_path):
    # An excluded stay with a death, and one clean included stay.
    # The original adjusted mortality dropped the excluded stay from the denominator only (1/1).
    stays, comps = _load_rows(tmp_path, [
        stay(dob="01/01/1950", excluded="Yes", had_complication="Yes", complication="Death"),
        stay(dob="02/02/1950"),
    ])
    assert summarize(stays, comps)["kpis"]["mortality_rate"] == pytest.approx(0.5)
    adjusted = summarize(stays, comps, population="adjusted")["kpis"]
    assert adjusted["deaths"] == 0
    assert adjusted["mortality_rate"] == pytest.approx(0.0)


def test_readmission_window_includes_day_30_and_not_day_31(tmp_path):
    # Discharged March 3, 2020: readmitted April 2 is day 30, April 3 is day 31.
    stays, comps = _load_rows(tmp_path, [
        stay(dob="01/01/1950", had_complication="Yes", complication="Infection",
             during_index_stay="No", complication_admit_date="04/02/2020"),
        stay(dob="02/02/1950", had_complication="Yes", complication="Infection",
             during_index_stay="No", complication_admit_date="04/03/2020"),
    ])
    k = summarize(stays, comps)["kpis"]
    assert k["readmit30"] == 1
    assert k["readmit30_rate"] == pytest.approx(0.5)


def test_age_is_at_admission_not_discharge(tmp_path):
    # Turns 65 on March 2, during a March 1-3 stay: 64 at admission, 65 at discharge.
    stays, _ = _load_rows(tmp_path, [stay(dob="03/02/1955")])
    assert stays.loc[0, "age"] == 64
    assert stays.loc[0, "age_group"] == "Under 65"


def test_age_groups_cover_boundary_ages(tmp_path):
    # Exactly 65, exactly 90 and 100 at admission (March 1, 2020).
    # The original bands skipped exactly 65, mislabeled 90-94 and had no band for 100+.
    stays, comps = _load_rows(tmp_path, [
        stay(dob="03/01/1955"),
        stay(dob="03/01/1930"),
        stay(dob="03/01/1920"),
    ])
    assert stays["age"].tolist() == [65, 90, 100]
    by_age = dict(zip(summarize(stays, comps)["by_age"]["group"], summarize(stays, comps)["by_age"]["stays"]))
    assert by_age["65–69"] == 1
    assert by_age["85+"] == 2
    assert sum(by_age.values()) == 3


# ---------- observed / expected (age group and procedure) ----------

def _comp(**kw):
    return stay(had_complication="Yes", complication="Infection", during_index_stay="Yes", **kw)


def test_oe_ratio_removes_a_difference_that_is_only_patient_mix(tmp_path):
    # Same within-group rates, different age mix. Reference: age 80-84 knee 2/4 = 50%, age 65-69 knee 0/2.
    # A: two 80-year-olds, one complication -> observed 50%, expected 1.0, O/E 1.0
    # B: two 80-year-olds (one complication) and two 66-year-olds -> observed 25%, expected 1.0, O/E 1.0
    stays, _ = _load_rows(tmp_path, [
        _comp(hospital="A", dob="01/01/1940"), stay(hospital="A", dob="01/02/1940"),
        _comp(hospital="B", dob="01/01/1940"), stay(hospital="B", dob="01/02/1940"),
        stay(hospital="B", dob="01/01/1954"), stay(hospital="B", dob="01/02/1954"),
    ])
    rows = {r["hospital"]: r for r in hospital_table(stays)}
    assert rows["A"]["complication_rate"] == pytest.approx(0.5)
    assert rows["B"]["complication_rate"] == pytest.approx(0.25)
    for h in ("A", "B"):
        assert rows[h]["expected"] == pytest.approx(1.0)
        assert rows[h]["oe_ratio"] == pytest.approx(1.0)
        assert rows[h]["adjusted_rate"] == pytest.approx(2 / 6)


def test_oe_ratio_shows_a_difference_within_the_same_patient_mix(tmp_path):
    # Four 66-year-old knee patients; A has two complications, B none. Reference rate 50%.
    stays, _ = _load_rows(tmp_path, [
        _comp(hospital="A", dob="01/01/1954"), _comp(hospital="A", dob="01/02/1954"),
        stay(hospital="B", dob="01/01/1954"), stay(hospital="B", dob="01/02/1954"),
    ])
    rows = {r["hospital"]: r for r in hospital_table(stays)}
    assert rows["A"]["oe_ratio"] == pytest.approx(2.0)
    assert rows["B"]["oe_ratio"] == pytest.approx(0.0)
    assert rows["B"]["oe_ci"][0] == 0.0


def test_oe_interval_matches_exact_poisson_limits():
    # Exact Poisson 95% limits: 0 events -> (0, 3.689); 10 events -> (4.795, 18.39)
    low, high = poisson_ratio_interval(0, 1)
    assert low == 0.0 and high == pytest.approx(3.689, rel=0.01)
    low, high = poisson_ratio_interval(10, 10)
    assert low == pytest.approx(0.4795, rel=0.01)
    assert high == pytest.approx(1.839, rel=0.01)
    assert poisson_ratio_interval(3, 0) == (None, None)


# ---------- complication detail: rates by type, timing, length of stay ----------

def test_rate_by_type_is_per_surgery(tmp_path):
    # Four surgeries: one has two infection records (counts once), one has pneumonia.
    stays, comps = _load_rows(tmp_path, [
        _comp(dob="01/01/1950"),
        _comp(dob="01/01/1950", additional_record="Yes"),
        stay(dob="02/02/1950", had_complication="Yes", complication="Pneumonia", during_index_stay="Yes"),
        stay(dob="03/03/1950"), stay(dob="04/04/1950"),
    ])
    rows = {r["type"]: r for r in summarize(stays, comps)["rate_by_type"]}
    assert rows["Infection"]["stays"] == 1
    assert rows["Infection"]["rate"] == pytest.approx(0.25)
    assert rows["Pneumonia"]["rate"] == pytest.approx(0.25)
    assert rows["AMI"]["rate"] == pytest.approx(0.0)
    low, high = rows["Infection"]["ci"]
    assert low < 0.25 < high


def test_timing_splits_during_stay_and_after_discharge(tmp_path):
    stays, comps = _load_rows(tmp_path, [
        _comp(dob="01/01/1950"),                                                     # infection during stay
        stay(dob="02/02/1950", had_complication="Yes", complication="Infection",
             during_index_stay="No", complication_admit_date="03/20/2020"),          # infection after discharge
        stay(dob="03/03/1950", had_complication="Yes", complication="Pneumonia",
             during_index_stay="No", complication_admit_date="03/06/2020"),          # pneumonia after discharge
    ])
    t = summarize(stays, comps)["timing_by_type"]
    timing = {typ: (d, a) for typ, d, a in zip(t["type"], t["during"], t["after"])}
    assert timing["Infection"] == (1, 1)
    assert timing["Pneumonia"] == (0, 1)
    assert timing["AMI"] == (0, 0)


def test_length_of_stay_by_complication_status(tmp_path):
    # No complication: 2 days. During stay: 6 and 4 days (one also has a later readmission) -> 5.
    # After discharge only: 2 days.
    stays, comps = _load_rows(tmp_path, [
        stay(dob="01/01/1950"),
        _comp(dob="02/02/1950", discharge="03/07/2020"),
        _comp(dob="03/03/1950", discharge="03/05/2020"),
        stay(dob="03/03/1950", discharge="03/05/2020", had_complication="Yes", complication="Sepsis",
             additional_record="Yes", during_index_stay="No", complication_admit_date="03/10/2020"),
        stay(dob="04/04/1950", had_complication="Yes", complication="Infection",
             during_index_stay="No", complication_admit_date="03/20/2020"),
    ])
    los = summarize(stays, comps)["los_by_status"]
    result = dict(zip(los["status"], zip(los["stays"], los["avg_los"])))
    assert result["No complication"] == (1, pytest.approx(2.0))
    assert result["Complication during stay"] == (2, pytest.approx(5.0))
    assert result["Complication after discharge only"] == (1, pytest.approx(2.0))


# ---------- hip vs knee odds ratios ----------

def test_odds_ratio_and_woolf_interval():
    # 10/100 hips vs 5/100 knees: OR = (10*95)/(90*5) = 2.111, Woolf 95% CI 0.695 to 6.42
    ratio, low, high = odds_ratio(10, 90, 5, 95)
    assert ratio == pytest.approx(2.111, rel=0.001)
    assert low == pytest.approx(0.6946, rel=0.01)
    assert high == pytest.approx(6.416, rel=0.01)
    # A zero cell gets 0.5 added to every cell: (0.5*5.5)/(10.5*5.5) = 0.0476
    assert odds_ratio(0, 10, 5, 5)[0] == pytest.approx(0.5 / 10.5)
    assert odds_ratio(0, 0, 5, 5) == (None, None, None)


def test_hip_vs_knee_leaves_out_stays_with_both_procedures(tmp_path):
    # Hips: 2 (one infection). Knees: 2 (none). One stay with both procedures and an infection is left out.
    # 2x2 = 1, 1, 0, 2 -> zero cell -> (1.5*2.5)/(1.5*0.5) = 5.0
    stays, comps = _load_rows(tmp_path, [
        _comp(dob="01/01/1950", tka=0, tha=1), stay(dob="02/02/1950", tka=0, tha=1),
        stay(dob="03/03/1950"), stay(dob="04/04/1950"),
        _comp(dob="05/05/1950", tka=1, tha=1),
    ])
    rows = {r["type"]: r for r in hip_vs_knee_odds(stays, comps)}
    anyc = rows["Any complication"]
    assert (anyc["hip"], anyc["hip_n"], anyc["knee"], anyc["knee_n"]) == (1, 2, 0, 2)
    assert anyc["odds_ratio"] == pytest.approx(5.0)
    assert rows["Infection"]["odds_ratio"] == pytest.approx(5.0)


def test_synthetic_in_stay_complications_lengthen_the_stay():
    stays, comps = build_stays(load(ROOT / "data" / "synthetic_discharges.csv"))
    los = dict(zip(*[summarize(stays, comps)["los_by_status"][k] for k in ("status", "avg_los")]))
    assert los["Complication during stay"] > los["No complication"] + 1


# ---------- build-time data checks ----------

def test_readmission_before_discharge_is_not_a_30_day_readmission(tmp_path):
    # Discharged March 3; a readmission dated March 1 is a data error, not day -2.
    stays, comps = _load_rows(tmp_path, [
        stay(dob="01/01/1950", had_complication="Yes", complication="Infection",
             during_index_stay="No", complication_admit_date="03/01/2020"),
        stay(dob="02/02/1950"),
    ])
    k = summarize(stays, comps)["kpis"]
    assert k["readmit30"] == 0
    assert k["complications"] == 1          # still a complication, just not a valid readmission


def test_build_stops_on_data_errors_unless_skipped(tmp_path, capsys):
    import build
    path = tmp_path / "bad.csv"
    make_records([
        stay(dob="01/01/1950", had_complication="Yes", complication="Infection",
             during_index_stay="No", complication_admit_date="03/01/2020"),
        stay(dob="02/02/1950"),
    ]).to_csv(path, index=False)
    out = tmp_path / "site"
    with pytest.raises(SystemExit) as stopped:
        build.main(["--data", str(path), "--out", str(out)])
    assert "Stopped" in str(stopped.value)
    assert not (out / "index.html").exists()
    build.main(["--data", str(path), "--out", str(out), "--skip-checks"])
    assert (out / "index.html").exists()


def test_synthetic_under_65_are_excluded():
    stays, _ = build_stays(load(ROOT / "data" / "synthetic_discharges.csv"))
    under = stays[stays["age"] < 65]
    assert len(under) > 0
    assert under["excluded"].all()


# ---------- age-adjusted (Mantel-Haenszel) odds ratio ----------

def test_mantel_haenszel_removes_confounding_by_age():
    # Within each age group hips and knees have the same odds (OR 1), but hips are mostly older,
    # and older patients have more complications.
    young = (1, 9, 4, 36)       # hips 1/10, knees 4/40 -> OR (1*36)/(9*4) = 1
    old = (20, 20, 5, 5)        # hips 20/40, knees 5/10 -> OR (20*5)/(20*5) = 1
    crude = odds_ratio(21, 29, 9, 41)[0]          # pooled: (21*41)/(29*9) = 3.30
    assert crude == pytest.approx(861 / 261)
    assert mantel_haenszel([young, old])[0] == pytest.approx(1.0)


def test_mantel_haenszel_with_one_stratum_matches_woolf():
    assert mantel_haenszel([(10, 90, 5, 95)]) == pytest.approx(odds_ratio(10, 90, 5, 95))
    assert mantel_haenszel([(0, 10, 0, 10)]) == (None, None, None)


def test_hip_vs_knee_age_adjusted(tmp_path):
    # Hips: two 80-year-olds, one complication. Knees: two 80-year-olds (one complication), two 66-year-olds (none).
    # Crude 2x2 = 1, 1, 1, 3 -> OR 3.0. Within the 80-84 group hips and knees are both 1 of 2 -> age-adjusted OR 1.0.
    stays, comps = _load_rows(tmp_path, [
        _comp(dob="01/01/1940", tka=0, tha=1), stay(dob="01/02/1940", tka=0, tha=1),
        _comp(dob="01/03/1940"), stay(dob="01/04/1940"),
        stay(dob="01/01/1954"), stay(dob="01/02/1954"),
    ])
    anyc = {r["type"]: r for r in hip_vs_knee_odds(stays, comps)}["Any complication"]
    assert anyc["odds_ratio"] == pytest.approx(3.0)
    assert anyc["age_adjusted_odds_ratio"] == pytest.approx(1.0)


# ---------- year trend interval ----------

def test_year_trend_has_a_confidence_interval(tmp_path):
    stays, comps = _load_rows(tmp_path, [_comp(dob="01/01/1950")] + [stay(dob=f"02/{d:02d}/1950") for d in range(1, 4)])
    y = summarize(stays, comps)["by_year"]
    assert y["complications"] == [1] and y["complication_rate"] == [pytest.approx(0.25)]
    assert y["complication_ci"][0] == list(wilson_interval(1, 4))


# ---------- small-cell suppression ----------

def _suppression_case(tmp_path):
    # 15 surgeries at one hospital: 3 with an infection, none with a death.
    rows = [_comp(dob=f"01/{d:02d}/1950") for d in range(1, 4)] + [stay(dob=f"02/{d:02d}/1950") for d in range(1, 13)]
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
    make_records([_comp(dob=f"01/{d:02d}/1950") for d in range(1, 4)] +
                 [stay(dob=f"02/{d:02d}/1950") for d in range(1, 13)]).to_csv(path, index=False)
    build.main(["--data", str(path), "--out", str(tmp_path / "site"), "--suppress-under", "11"])
    assert '"suppressed_under":11' in (tmp_path / "site" / "index.html").read_text()
