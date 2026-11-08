"""Synthetic-only guard: decide whether a file is this project's synthetic data.

Every file the synthetic generator writes carries a marker column with one fixed
value on every row, and uses only the generator's three fictional hospital names.
The data-check Lambda refuses (and deletes) any upload that fails this check, and
CI refuses any committed CSV that fails it.

What it is and isn't: it reliably catches the realistic mistake, uploading the
real discharge export instead of the synthetic one, because the real export has
no marker column and real hospital names. It is not proof: anyone can add the
column to a real file on purpose. The rule that real data is never uploaded is
a rule for people; this is the safety net for accidents.

Standard library only (no pandas), so it runs before anything is installed and
reads the file without ever building a data frame from it. Reasons are fixed
strings and never include a value from the file.
"""

import csv
from pathlib import Path

MARKER_COLUMN = "Synthetic Marker"
MARKER_VALUE = "SYNTHETIC - NOT REAL PATIENT DATA"
HOSPITAL_COLUMN = "Hospital"
# Must match outcomes.synthetic.HOSPITALS (a test checks this)
FICTIONAL_HOSPITALS = frozenset({"Hospital A", "Hospital B", "Hospital C"})

NOT_CSV = "not a CSV file"
NOT_TEXT = "not readable as UTF-8 text"
NO_MARKER_COLUMN = "no synthetic marker column"
ROW_WITHOUT_MARKER = "a row without the synthetic marker"
NO_HOSPITAL_COLUMN = "no Hospital column"
NON_FICTIONAL_HOSPITAL = "a hospital that is not one of the synthetic dataset's fictional hospitals"
UNREADABLE = "not readable as CSV"


def check(path: Path | str) -> str | None:
    """None if the file looks like this project's synthetic data, else the reason it doesn't.

    The marker must be in the first row's headers (the synthetic file has no
    preamble rows), and every non-blank row must carry the marker value and a
    fictional hospital name.
    """
    path = Path(path)
    if path.suffix.lower() != ".csv":
        return NOT_CSV
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            headers = [h.strip() for h in next(reader, [])]
            if MARKER_COLUMN not in headers:
                return NO_MARKER_COLUMN
            if HOSPITAL_COLUMN not in headers:
                return NO_HOSPITAL_COLUMN
            marker, hospital = headers.index(MARKER_COLUMN), headers.index(HOSPITAL_COLUMN)
            for row in reader:
                if not any(cell.strip() for cell in row):
                    continue   # blank line
                if len(row) <= marker or row[marker].strip() != MARKER_VALUE:
                    return ROW_WITHOUT_MARKER
                if len(row) <= hospital or row[hospital].strip() not in FICTIONAL_HOSPITALS:
                    return NON_FICTIONAL_HOSPITAL
    except UnicodeDecodeError:
        return NOT_TEXT
    except csv.Error:
        return UNREADABLE
    return None
