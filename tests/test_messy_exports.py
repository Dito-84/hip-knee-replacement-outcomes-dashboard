"""Messy-export problems that used to change a rate without any warning (review of November 10, 2026).

Each test is a small made-up file where the right answer is known.
"""

import pytest

from helpers import make_records, stay
from outcomes.load import load
from outcomes.metrics import build_stays, filter_stays, kpis
from outcomes.quality import run_checks


def write(tmp_path, rows):
    path = tmp_path / "case.csv"
    make_records(rows).to_csv(path, index=False)
    return load(path)


def checks(records):
    return {f.check: f for f in run_checks(records)}


def readmitted(dob, admit_date):
    return stay(dob=dob, had_complication="Yes", complication="Infection", during_index_stay="No",
                complication_admit_date=admit_date)


# ---------- Dates ----------

def test_iso_dates_count_like_us_dates(tmp_path):
    """Two readmissions 7 days after discharge, one written 03/10/2020 and one 2020-03-10.
    pandas used to infer the format from the first value and blank out the other: 1 readmission, not 2."""
    records = write(tmp_path, [readmitted("01/01/1950", "03/10/2020"), readmitted("02/02/1950", "2020-03-10")])
    stays, _ = build_stays(records)
    k = kpis(filter_stays(stays), "all")
    assert k["readmit30"] == 2 and k["readmit30_rate"] == 1.0
    assert run_checks(records) == []


def test_a_date_that_cant_be_read_is_an_error_not_a_blank(tmp_path):
    records = write(tmp_path, [readmitted("01/01/1950", "03/10/2020"), readmitted("02/02/1950", "10 March 2020"),
                               readmitted("03/03/1950", "03/40/2020")])
    found = checks(records)["unreadable date"]
    assert found.severity == "error" and found.count == 2
    assert "Admit Date for Complication" in found.message
    assert "March" not in found.message and "03/40" not in found.message   # counts only, never a value


def test_blank_optional_dates_are_fine(tmp_path):
    records = write(tmp_path, [stay(dob="01/01/1950"), stay(dob="02/02/1950", death_date="  ")])
    assert "unreadable date" not in checks(records)


# ---------- Yes/No ----------

def test_blank_or_unknown_excluded_is_an_error_not_included(tmp_path):
    """Excluded = Yes, blank, Unknown. Both of the last two used to count as included (No),
    putting them in the exclusion-adjusted view's denominator."""
    records = write(tmp_path, [stay(dob="01/01/1950", excluded="Yes"), stay(dob="02/02/1950", excluded=""),
                               stay(dob="03/03/1950", excluded="Unknown")])
    found = checks(records)["unreadable Yes/No"]
    assert found.severity == "error" and found.count == 2
    assert "Excluded (Yes/No)" in found.message and "Unknown" not in found.message


@pytest.mark.parametrize("value, excluded", [("Yes", True), ("y", True), ("TRUE", True), ("1", True),
                                             ("1.0", True), ("No", False), ("n", False), ("0", False)])
def test_recognized_yes_no_spellings(tmp_path, value, excluded):
    records = write(tmp_path, [stay(excluded=value)])
    assert bool(records.loc[0, "excluded"]) is excluded
    assert run_checks(records) == []


def test_blank_is_no_in_an_optional_yes_no(tmp_path):
    records = write(tmp_path, [stay(covid_excluded="", additional_record="")])
    assert run_checks(records) == []


# ---------- One surgery, one stay ----------

def test_an_additional_record_that_matches_no_stay_is_an_error(tmp_path):
    """One surgery, two complications; the second record's discharge date is a day off.
    It used to become a second stay: 2 surgeries instead of 1, and no finding."""
    first = stay(dob="01/01/1950", had_complication="Yes", complication="Infection", during_index_stay="Yes")
    second = stay(dob="01/01/1950", discharge="03/04/2020", had_complication="Yes", complication="Sepsis",
                  during_index_stay="Yes", additional_record="Yes")
    found = checks(write(tmp_path, [first, second]))["additional record without its stay"]
    assert found.severity == "error" and found.count == 1


def test_a_matching_additional_record_is_one_stay(tmp_path):
    first = stay(dob="01/01/1950", had_complication="Yes", complication="Infection", during_index_stay="Yes")
    second = {**first, "complication": "Sepsis", "additional_record": "Yes"}
    records = write(tmp_path, [first, second])
    assert run_checks(records) == []
    stays, _ = build_stays(records)
    assert len(stays) == 1


def test_records_of_one_stay_that_disagree_are_flagged(tmp_path):
    first = stay(dob="01/01/1950", had_complication="Yes", complication="Infection", during_index_stay="Yes")
    second = {**first, "complication": "Sepsis", "additional_record": "Yes", "excluded": "Yes"}
    found = checks(write(tmp_path, [first, second]))["stay details differ between records"]
    assert found.severity == "warning" and found.count == 2


# ---------- Deaths and readmissions ----------

def test_a_death_date_on_another_complication_is_flagged(tmp_path):
    """Mortality counts Death records; a death date on, say, a Sepsis record wouldn't be counted."""
    records = write(tmp_path, [stay(had_complication="Yes", complication="Sepsis", during_index_stay="No",
                                    complication_admit_date="03/05/2020", death_date="03/09/2020")])
    assert checks(records)["death date on another complication"].count == 1


def test_a_death_before_admission_is_flagged(tmp_path):
    records = write(tmp_path, [stay(had_complication="Yes", complication="Death", during_index_stay="No",
                                    death_date="02/01/2020")])
    assert checks(records)["death before admission"].count == 1


def test_an_after_discharge_complication_without_a_date_is_flagged(tmp_path):
    records = write(tmp_path, [stay(had_complication="Yes", complication="Pneumonia", during_index_stay="No")])
    found = checks(records)["complication after discharge without a date"]
    assert found.severity == "warning" and found.count == 1
