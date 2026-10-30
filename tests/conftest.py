import pytest

from helpers import make_records, stay
from outcomes.load import load


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
