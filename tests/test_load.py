"""Loading: header matching, header-row detection and file types."""

import pandas as pd
import pytest

from helpers import make_records, stay
from outcomes.load import DataFormatError, load
from outcomes.schema import match_headers


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
