"""Measures counted per surgery: rates, readmission window, filters, length of stay, age, timing."""

import pandas as pd
import pytest

from helpers import ROOT, complication_stay, load_rows, stay
from outcomes.load import load
from outcomes.metrics import build_dashboard_data, build_stays, summarize
from outcomes.stats import wilson_interval


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


def test_rate_by_type_is_per_surgery(tmp_path):
    # Four surgeries: one has two infection records (counts once), one has pneumonia.
    stays, comps = load_rows(tmp_path, [
        complication_stay(dob="01/01/1950"),
        complication_stay(dob="01/01/1950", additional_record="Yes"),
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
    stays, comps = load_rows(tmp_path, [
        complication_stay(dob="01/01/1950"),                                                     # infection during stay
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
    stays, comps = load_rows(tmp_path, [
        stay(dob="01/01/1950"),
        complication_stay(dob="02/02/1950", discharge="03/07/2020"),
        complication_stay(dob="03/03/1950", discharge="03/05/2020"),
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


def test_readmission_before_discharge_is_not_a_30_day_readmission(tmp_path):
    # Discharged March 3; a readmission dated March 1 is a data error, not day -2.
    stays, comps = load_rows(tmp_path, [
        stay(dob="01/01/1950", had_complication="Yes", complication="Infection",
             during_index_stay="No", complication_admit_date="03/01/2020"),
        stay(dob="02/02/1950"),
    ])
    k = summarize(stays, comps)["kpis"]
    assert k["readmit30"] == 0
    assert k["complications"] == 1          # still a complication, just not a valid readmission


def test_year_trend_has_a_confidence_interval(tmp_path):
    rows = [complication_stay(dob="01/01/1950")] + [stay(dob=f"02/{d:02d}/1950") for d in range(1, 4)]
    stays, comps = load_rows(tmp_path, rows)
    y = summarize(stays, comps)["by_year"]
    assert y["complications"] == [1] and y["complication_rate"] == [pytest.approx(0.25)]
    assert y["complication_ci"][0] == list(wilson_interval(1, 4))


def test_dashboard_data_has_every_view():
    data = build_dashboard_data(load(ROOT / "data" / "synthetic_discharges.csv"), "synthetic")
    hospitals = data["hospitals"]
    assert hospitals[0] == "All"
    expected = len(hospitals) * 3 * 2 + 3 * 2
    assert len(data["views"]) == expected
