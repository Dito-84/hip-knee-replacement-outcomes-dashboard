from pathlib import Path

import pandas as pd
import pytest

from outcomes import synthetic
from outcomes.load import DataFormatError, load
from outcomes.metrics import build_dashboard_data, build_stays, summarize, wilson_interval
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
