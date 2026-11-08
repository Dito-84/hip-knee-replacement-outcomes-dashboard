"""Sample batches for the upload page, each built from the synthetic generator.

Each one demonstrates one outcome of the AWS data check, and a test checks it
gets exactly that outcome:

- clean:            passes, with no drift alerts
- quality-problems: fails the quality checks (five discharges dated before admission)
- drift:            passes the guard but raises drift alerts (counts sent as Yes/No,
                    a new Measure code on 10% of rows, Excluded left blank on 20%)
- no-marker:        the clean batch without the synthetic marker column, which the
                    synthetic-only guard rejects and deletes

They're generated at build time into the site's samples/ folder, never committed:
the no-marker file would (correctly) fail CI's no-real-data check.
"""

from pathlib import Path

import pandas as pd

from .synthetic import generate
from .synthetic_guard import MARKER_COLUMN

SEED, STAYS = 13, 1000


def _clean() -> pd.DataFrame:
    return generate(n_stays=STAYS, seed=SEED)


def _quality_problems() -> pd.DataFrame:
    df = _clean()
    admit = [c for c in df.columns if c.startswith("Admission Date")][0]
    discharge = [c for c in df.columns if c.startswith("Discharge Date")][0]
    rows = df.index[:5]
    df.loc[rows, discharge] = (pd.to_datetime(df.loc[rows, admit]) - pd.Timedelta(days=3)).dt.strftime("%m/%d/%Y")
    return df


def _drift() -> pd.DataFrame:
    df = _clean()
    tka = [c for c in df.columns if c.startswith("Number of TKAs")][0]
    df[tka] = df[tka].map({"0": "No", "1": "Yes", "2": "Yes", 0: "No", 1: "Yes", 2: "Yes"})
    df.loc[df.index % 10 == 0, "Measure"] = "THA/TKA Readmission"
    df.loc[df.index % 5 == 1, "Excluded (Yes/No)"] = ""
    return df


def _no_marker() -> pd.DataFrame:
    return _clean().drop(columns=[MARKER_COLUMN])


SAMPLES = {
    "clean": ("Clean batch", "Passes every check", _clean),
    "quality-problems": ("Quality problems", "Fails: five discharges dated before admission", _quality_problems),
    "drift": ("Drifted batch", "Raises drift alerts, plus a quality error from the miscoded counts", _drift),
    "no-marker": ("No synthetic marker", "Rejected and deleted by the synthetic-only guard", _no_marker),
}


def write_samples(folder: Path) -> list[dict]:
    """Write every sample as folder/sample-<name>.csv; return what the upload page lists."""
    folder.mkdir(parents=True, exist_ok=True)
    listed = []
    for name, (title, outcome, make) in SAMPLES.items():
        path = folder / f"sample-{name}.csv"
        make().to_csv(path, index=False)
        listed.append({"file": path.name, "title": title, "outcome": outcome})
    return listed
