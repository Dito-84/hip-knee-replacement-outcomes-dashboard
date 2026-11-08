"""The synthetic-only guard, on small made-up files where the right answer is known."""

import pytest

from helpers import ROOT
from outcomes import synthetic
from outcomes import synthetic_guard as guard
from outcomes.load import load
from outcomes.schema import COLUMNS

M = guard.MARKER_VALUE


def write(tmp_path, text, name="batch.csv", encoding="utf-8"):
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


def test_committed_synthetic_file_passes():
    assert guard.check(ROOT / "data" / "synthetic_discharges.csv") is None


def test_generator_marks_every_row_and_the_loader_drops_the_marker(tmp_path):
    df = synthetic.generate(n_stays=50, seed=7)
    assert (df[guard.MARKER_COLUMN] == M).all()
    path = tmp_path / "fresh.csv"
    df.to_csv(path, index=False)
    assert guard.check(path) is None
    assert list(load(path).columns) == list(COLUMNS)   # same columns the dashboard always had


def test_fictional_hospitals_match_the_generator():
    assert guard.FICTIONAL_HOSPITALS == set(synthetic.HOSPITALS)


def test_a_small_marked_file_passes_with_blank_lines_and_a_byte_order_mark(tmp_path):
    text = f"﻿Hospital,Measure,Synthetic Marker\nHospital A,x,{M}\n\nHospital C,y,{M}\n"
    assert guard.check(write(tmp_path, text)) is None


@pytest.mark.parametrize("text, reason", [
    # The realistic accident: the real export has no marker column at all
    ("Hospital,Measure\nHospital A,x\n", guard.NO_MARKER_COLUMN),
    # Marker present but missing, wrong or blank on one row
    (f"Hospital,Synthetic Marker\nHospital A,{M}\nHospital B,synthetic\n", guard.ROW_WITHOUT_MARKER),
    (f"Hospital,Synthetic Marker\nHospital A,{M}\nHospital B,\n", guard.ROW_WITHOUT_MARKER),
    (f"Hospital,Measure,Synthetic Marker\nHospital A,x,{M}\nHospital B,x\n", guard.ROW_WITHOUT_MARKER),
    # A hospital name that isn't one of the generator's three
    (f"Hospital,Synthetic Marker\nHospital A,{M}\nGeneral Hospital,{M}\n", guard.NON_FICTIONAL_HOSPITAL),
    (f"Synthetic Marker,Measure\n{M},x\n", guard.NO_HOSPITAL_COLUMN),
    # The marker must be in the first row: no preamble rows above the headers
    (f"Exported 2023\nHospital,Synthetic Marker\nHospital A,{M}\n", guard.NO_MARKER_COLUMN),
    ("", guard.NO_MARKER_COLUMN),
])
def test_files_without_the_marker_or_with_other_hospitals_are_refused(tmp_path, text, reason):
    assert guard.check(write(tmp_path, text)) == reason


def test_only_csv_files_are_accepted(tmp_path):
    text = f"Hospital,Synthetic Marker\nHospital A,{M}\n"
    for name in ("batch.xlsx", "batch.txt", "batch"):
        assert guard.check(write(tmp_path, text, name=name)) == guard.NOT_CSV


def test_binary_or_non_utf8_files_are_refused(tmp_path):
    path = tmp_path / "batch.csv"
    path.write_bytes(b"PK\x03\x04\xff\xfe\x00 an Excel file renamed to .csv")
    assert guard.check(path) == guard.NOT_TEXT


def test_reasons_never_contain_values_from_the_file(tmp_path):
    secret = "Real Memorial Hospital"
    path = write(tmp_path, f"Hospital,Synthetic Marker\n{secret},{M}\n")
    reason = guard.check(path)
    assert reason and secret not in reason and "Real" not in reason
