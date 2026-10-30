"""Shared helpers for building small made-up discharge files in the source format."""

from pathlib import Path

import pandas as pd

from outcomes.load import load
from outcomes.metrics import build_stays
from outcomes.schema import COLUMNS

ROOT = Path(__file__).resolve().parent.parent
SRC = {name: src for name, (src, _) in COLUMNS.items()}


def make_records(rows):
    """Build a small source-format table: each row is a dict of internal names."""
    df = pd.DataFrame(rows).reindex(columns=list(COLUMNS)).astype(object).fillna("")
    return df.rename(columns=SRC)


def stay(hospital="Hospital A", dob="01/01/1950", admit="03/01/2020", discharge="03/03/2020",
         tka=1, tha=0, excluded="No", **extra):
    row = {"hospital": hospital, "beneficiary_dob": dob, "admission_date": admit, "discharge_date": discharge,
           "n_tka": tka, "n_tha": tha, "excluded": excluded, "had_complication": "No", "complication": "",
           "additional_record": "No", "covid_excluded": "No"}
    row.update(extra)
    return row


def load_rows(tmp_path, rows):
    """Write made-up rows to a CSV, load it like a real export and collapse it to stays."""
    path = tmp_path / "case.csv"
    make_records(rows).to_csv(path, index=False)
    return build_stays(load(path))


def complication_stay(**kw):
    """A stay with an infection during the index stay."""
    return stay(had_complication="Yes", complication="Infection", during_index_stay="Yes", **kw)
