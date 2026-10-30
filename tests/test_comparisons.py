"""Comparisons: hospital observed vs expected, and hip vs knee odds ratios (unadjusted and age-adjusted)."""

import pytest

from helpers import complication_stay, load_rows, stay
from outcomes.metrics import hip_vs_knee_odds, hospital_table


def test_oe_ratio_removes_a_difference_that_is_only_patient_mix(tmp_path):
    # Same within-group rates, different age mix. Reference: age 80-84 knee 2/4 = 50%, age 65-69 knee 0/2.
    # A: two 80-year-olds, one complication -> observed 50%, expected 1.0, O/E 1.0
    # B: two 80-year-olds (one complication) and two 66-year-olds -> observed 25%, expected 1.0, O/E 1.0
    stays, _ = load_rows(tmp_path, [
        complication_stay(hospital="A", dob="01/01/1940"), stay(hospital="A", dob="01/02/1940"),
        complication_stay(hospital="B", dob="01/01/1940"), stay(hospital="B", dob="01/02/1940"),
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
    stays, _ = load_rows(tmp_path, [
        complication_stay(hospital="A", dob="01/01/1954"), complication_stay(hospital="A", dob="01/02/1954"),
        stay(hospital="B", dob="01/01/1954"), stay(hospital="B", dob="01/02/1954"),
    ])
    rows = {r["hospital"]: r for r in hospital_table(stays)}
    assert rows["A"]["oe_ratio"] == pytest.approx(2.0)
    assert rows["B"]["oe_ratio"] == pytest.approx(0.0)
    assert rows["B"]["oe_ci"][0] == 0.0


def test_hip_vs_knee_leaves_out_stays_with_both_procedures(tmp_path):
    # Hips: 2 (one infection). Knees: 2 (none). One stay with both procedures and an infection is left out.
    # 2x2 = 1, 1, 0, 2 -> zero cell -> (1.5*2.5)/(1.5*0.5) = 5.0
    stays, comps = load_rows(tmp_path, [
        complication_stay(dob="01/01/1950", tka=0, tha=1), stay(dob="02/02/1950", tka=0, tha=1),
        stay(dob="03/03/1950"), stay(dob="04/04/1950"),
        complication_stay(dob="05/05/1950", tka=1, tha=1),
    ])
    rows = {r["type"]: r for r in hip_vs_knee_odds(stays, comps)}
    anyc = rows["Any complication"]
    assert (anyc["hip"], anyc["hip_n"], anyc["knee"], anyc["knee_n"]) == (1, 2, 0, 2)
    assert anyc["odds_ratio"] == pytest.approx(5.0)
    assert rows["Infection"]["odds_ratio"] == pytest.approx(5.0)


def test_hip_vs_knee_age_adjusted(tmp_path):
    # Hips: two 80-year-olds, one complication. Knees: two 80-year-olds (one complication), two 66-year-olds (none).
    # Crude 2x2 = 1, 1, 1, 3 -> OR 3.0. Within the 80-84 group hips and knees are both 1 of 2 -> age-adjusted OR 1.0.
    stays, comps = load_rows(tmp_path, [
        complication_stay(dob="01/01/1940", tka=0, tha=1), stay(dob="01/02/1940", tka=0, tha=1),
        complication_stay(dob="01/03/1940"), stay(dob="01/04/1940"),
        stay(dob="01/01/1954"), stay(dob="01/02/1954"),
    ])
    anyc = {r["type"]: r for r in hip_vs_knee_odds(stays, comps)}["Any complication"]
    assert anyc["odds_ratio"] == pytest.approx(3.0)
    assert anyc["age_adjusted_odds_ratio"] == pytest.approx(1.0)
