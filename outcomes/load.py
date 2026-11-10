"""Read a discharge-level file (CSV or Excel) into a clean DataFrame.

Works with the synthetic CSV in data/ and with an original Excel export,
whose header row may not be the first row and whose headers may contain
line breaks.
"""

from pathlib import Path

import pandas as pd

from .schema import COLUMNS, COUNT_COLUMNS, DATE_COLUMNS, YES_NO_COLUMNS, match_headers


class DataFormatError(ValueError):
    pass


def _find_header_row(raw):
    """Index of the first row that contains the expected headers."""
    for i in range(min(len(raw), 25)):
        _, missing = match_headers(raw.iloc[i].astype(str).tolist())
        if not missing:
            return i
    return None


def _read_raw(path, sheet=None):
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return {"csv": pd.read_csv(path, header=None, dtype=object, keep_default_na=False)}
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path, sheet_name=sheet if sheet is not None else None, header=None, dtype=object,
                             engine="openpyxl")
    raise DataFormatError(f"Unsupported file type: {path.suffix} (use .csv or .xlsx)")


YES = {"yes", "y", "true", "1", "1.0"}
NO = {"no", "n", "false", "0", "0.0"}


def _yes_no(series, required=False):
    """(True/False per row, how many values were neither Yes nor No).

    Blank means No in an optional column (no complication, no readmission). Anything that isn't
    a recognizable Yes or No, and a blank in a required column, is counted rather than guessed:
    "Unknown" used to read as No, so an Excluded (Yes/No) of "Unknown" counted the stay as included.
    """
    text = series.fillna("").astype(str).str.strip().str.lower()
    allowed = YES | NO | (set() if required else {""})
    return text.isin(YES), int((~text.isin(allowed)).sum())


# Dates in the export are US month/day/year text in a CSV, or real dates in Excel. ISO dates
# (2020-03-10) are unambiguous, so they're read too. Anything else is counted, not guessed:
# pandas would otherwise infer one format from the first value and blank out the rest.
DATE_FORMATS = ("%m/%d/%Y", "ISO8601")


def _dates(series):
    """(dates, how many filled values couldn't be read as a date)."""
    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    for fmt in DATE_FORMATS:
        todo = parsed.isna()
        if todo.any():
            parsed[todo] = pd.to_datetime(series[todo], format=fmt, errors="coerce")
    filled = series.notna() & (series.astype("string").str.strip() != "")
    return parsed, int((filled & parsed.isna()).sum())


def _table(path, sheet=None):
    """The first sheet with the expected headers, as text, with every column it has.

    Known columns get their internal names; any others keep their header, stripped.
    Blank cells become missing and fully blank rows (common at the bottom of Excel
    exports) are dropped.
    """
    sheets = _read_raw(path, sheet)
    if isinstance(sheets, pd.DataFrame):
        sheets = {sheet: sheets}

    for raw in sheets.values():
        header_row = _find_header_row(raw)
        if header_row is None:
            continue
        headers = raw.iloc[header_row].astype(str).tolist()
        mapping, _ = match_headers(headers)
        df = raw.iloc[header_row + 1:].copy()
        df.columns = [mapping.get(h, " ".join(h.split())) for h in headers]
        df = df.loc[:, [c != "" and c.lower() not in ("nan", "none") for c in df.columns]]
        df = df.loc[:, ~df.columns.duplicated()]
        return df.replace(r"^\s*$", pd.NA, regex=True).dropna(how="all")

    _, missing = match_headers([])
    expected = ", ".join(COLUMNS[m][0] for m in missing)
    raise DataFormatError(f"No sheet in {path} has the expected headers: {expected}")


def load_raw(path, sheet=None):
    """The file's rows before any cleaning: every column, values as text, blanks missing.

    For checks that need to see the file as delivered (the drift check), since
    load() converts types, fills absent columns and drops unknown ones.
    """
    df = _table(path, sheet).astype("string")
    return df.apply(lambda col: col.str.strip()).reset_index(drop=True)


def load(path, sheet=None):
    """Load and clean a discharge file. Returns one row per complication record.

    `attrs["unreadable"]` counts, per column, the filled values that couldn't be read as a date or
    a Yes/No; quality.run_checks() reports them as errors.
    """
    df = _table(path, sheet)
    df = df[[c for c in df.columns if c in COLUMNS]]

    for col in COLUMNS:
        if col not in df:
            df[col] = pd.NA
    unreadable = {}   # column -> how many filled values couldn't be read (see quality.py)
    for col in DATE_COLUMNS:
        df[col], unreadable[col] = _dates(df[col])
    for col in YES_NO_COLUMNS:
        df[col], unreadable[col] = _yes_no(df[col], required=COLUMNS[col][1])
    for col in COUNT_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    for col in ("hospital", "complication", "inclusion_exclusion", "measure"):
        df[col] = df[col].astype("string").str.strip()

    bad = df["admission_date"].isna() | df["discharge_date"].isna()
    if bad.any():
        raise DataFormatError(f"{int(bad.sum())} rows are missing an admission or discharge date, "
                              "or have one that isn't a date")

    out = df[list(COLUMNS)].reset_index(drop=True)
    out.attrs["unreadable"] = {col: n for col, n in unreadable.items() if n}
    return out
