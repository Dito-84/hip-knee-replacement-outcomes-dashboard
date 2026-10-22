import sys
from pathlib import Path

import pandas as pd
import pytest

from outcomes.load import load
from outcomes.quality import run_checks

ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC = ROOT / "data" / "synthetic_discharges.csv"
sys.path.insert(0, str(ROOT / "scripts"))
import check_data  # noqa: E402


@pytest.fixture
def records():
    return load(SYNTHETIC)


def checks(df):
    return {f.check: f for f in run_checks(df)}


def test_synthetic_data_passes(records):
    assert run_checks(records) == []


def test_detects_date_problems(records):
    r = records.copy()
    r.loc[0, "discharge_date"] = r.loc[0, "admission_date"] - pd.Timedelta(days=2)
    comp = r.index[r["complication_admit_date"].notna()][0]
    r.loc[comp, "complication_admit_date"] = r.loc[comp, "discharge_date"] - pd.Timedelta(days=1)
    r.loc[1, "beneficiary_dob"] = r.loc[1, "admission_date"] - pd.Timedelta(days=365 * 10)
    found = checks(r)
    assert found["discharge before admission"].count == 1
    assert found["readmission before discharge"].count == 1
    assert found["implausible age"].count == 1
    assert all(found[k].severity == "error" for k in
               ("discharge before admission", "readmission before discharge", "implausible age"))


def test_detects_duplicates(records):
    r = pd.concat([records, records.iloc[[5]]], ignore_index=True)
    found = checks(r)
    assert found["exact duplicate rows"].count == 1
    assert found["unflagged repeat rows"].count == 1


def test_flagged_additional_records_are_fine(records):
    assert records["additional_record"].any()           # synthetic data has some
    assert "unflagged repeat rows" not in checks(records)


def test_detects_missing_procedure_and_unnamed_complication(records):
    r = records.copy()
    r.loc[0, ["n_tha", "n_tka"]] = 0
    r.loc[1, "had_complication"] = True
    r.loc[1, "complication"] = pd.NA
    found = checks(r)
    assert found["no procedure"].count == 1
    assert found["complication without type"].count == 1


def test_script_refuses_to_write_report_inside_repo(capsys):
    assert check_data.main([str(SYNTHETIC), "--out", str(ROOT / "docs" / "report.html")]) == 2
    assert "Refusing" in capsys.readouterr().out


def test_script_works_without_dstoolkit(monkeypatch, capsys, tmp_path):
    monkeypatch.setitem(sys.modules, "dstoolkit", None)   # simulate "not installed"
    assert check_data.main([str(SYNTHETIC), "--out", str(tmp_path / "check.html")]) == 0
    out = capsys.readouterr().out
    assert "All dataset checks passed" in out
    assert "install dstoolkit" in out
    assert not (tmp_path / "check.html").exists()


def test_script_exit_code_reflects_errors(tmp_path, records, capsys):
    bad = records.copy()
    bad.loc[0, "discharge_date"] = bad.loc[0, "admission_date"] - pd.Timedelta(days=1)
    path = tmp_path / "bad.csv"
    bad.rename(columns={"hospital": "Hospital", "measure": "Measure", "beneficiary_dob": "Beneficiary DOB",
                        "admission_date": "Admission Date of Index Stay",
                        "discharge_date": "Discharge Date of Index Stay",
                        "n_tka": "Number of TKAs Performed", "n_tha": "Number of THAs Performed",
                        "had_complication": "Patient Had a Complication", "excluded": "Excluded (Yes/No)",
                        "complication": "Complication"}).to_csv(path, index=False)
    monkeypatch_free = check_data.main([str(path), "--out", str(tmp_path / "x.html")])
    assert monkeypatch_free == 1
    assert "discharge before admission" in capsys.readouterr().out
