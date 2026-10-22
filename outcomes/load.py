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
        return pd.read_excel(path, sheet_name=sheet if sheet is not None else None, header=None, dtype=object)
    raise DataFormatError(f"Unsupported file type: {path.suffix} (use .csv or .xlsx)")


def _yes_no(series):
    text = series.astype(str).str.strip().str.lower()
    return text.isin(["yes", "y", "true", "1"])


def load(path, sheet=None):
    """Load and clean a discharge file. Returns one row per complication record."""
    sheets = _read_raw(path, sheet)
    if isinstance(sheets, pd.DataFrame):
        sheets = {sheet: sheets}

    for name, raw in sheets.items():
        header_row = _find_header_row(raw)
        if header_row is None:
            continue
        headers = raw.iloc[header_row].astype(str).tolist()
        mapping, _ = match_headers(headers)
        df = raw.iloc[header_row + 1:].copy()
        df.columns = headers
        df = df[[h for h in headers if h in mapping]].rename(columns=mapping)
        break
    else:
        _, missing = match_headers([])
        expected = ", ".join(COLUMNS[m][0] for m in missing)
        raise DataFormatError(f"No sheet in {path} has the expected headers: {expected}")

    # Drop fully blank rows (common at the bottom of Excel exports)
    df = df.replace(r"^\s*$", pd.NA, regex=True).dropna(how="all")

    for col in COLUMNS:
        if col not in df:
            df[col] = pd.NA
    for col in DATE_COLUMNS:
        df[col] = pd.to_datetime(df[col], errors="coerce")
    for col in YES_NO_COLUMNS:
        df[col] = _yes_no(df[col].fillna(""))
    for col in COUNT_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    for col in ("hospital", "complication", "inclusion_exclusion", "measure"):
        df[col] = df[col].astype("string").str.strip()

    bad = df["admission_date"].isna() | df["discharge_date"].isna()
    if bad.any():
        raise DataFormatError(f"{int(bad.sum())} rows are missing an admission or discharge date")

    return df[list(COLUMNS)].reset_index(drop=True)
