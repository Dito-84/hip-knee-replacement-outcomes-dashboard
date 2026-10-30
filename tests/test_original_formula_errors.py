"""Errors found in the original Qlik formulas. Each case is made up, with an answer known by hand,
and pins down that the rebuild doesn't repeat the mistake (docs/measure-crosswalk.md)."""

import pytest

from helpers import load_rows, stay
from outcomes.metrics import summarize


def test_knee_rates_use_knee_surgeries_as_denominator(tmp_path):
    # One knee surgery with an AMI, three clean hip surgeries.
    # The original knee AMI rate divided by hip admissions (1/3); the right answer is 1/1.
    stays, comps = load_rows(tmp_path, [
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
    stays, comps = load_rows(tmp_path, [
        stay(dob="01/01/1950", excluded="Yes", had_complication="Yes", complication="Death"),
        stay(dob="02/02/1950"),
    ])
    assert summarize(stays, comps)["kpis"]["mortality_rate"] == pytest.approx(0.5)
    adjusted = summarize(stays, comps, population="adjusted")["kpis"]
    assert adjusted["deaths"] == 0
    assert adjusted["mortality_rate"] == pytest.approx(0.0)


def test_readmission_window_includes_day_30_and_not_day_31(tmp_path):
    # Discharged March 3, 2020: readmitted April 2 is day 30, April 3 is day 31.
    stays, comps = load_rows(tmp_path, [
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
    stays, _ = load_rows(tmp_path, [stay(dob="03/02/1955")])
    assert stays.loc[0, "age"] == 64
    assert stays.loc[0, "age_group"] == "Under 65"


def test_age_groups_cover_boundary_ages(tmp_path):
    # Exactly 65, exactly 90 and 100 at admission (March 1, 2020).
    # The original bands skipped exactly 65, mislabeled 90-94 and had no band for 100+.
    stays, comps = load_rows(tmp_path, [
        stay(dob="03/01/1955"),
        stay(dob="03/01/1930"),
        stay(dob="03/01/1920"),
    ])
    assert stays["age"].tolist() == [65, 90, 100]
    by_age = dict(zip(summarize(stays, comps)["by_age"]["group"], summarize(stays, comps)["by_age"]["stays"]))
    assert by_age["65–69"] == 1
    assert by_age["85+"] == 2
    assert sum(by_age.values()) == 3
