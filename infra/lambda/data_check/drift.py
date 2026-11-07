"""Compare an uploaded file with the reference snapshot, using dstoolkit's compare().

Both files are read as delivered (outcomes.load.load_raw), not after cleaning,
so a count column arriving as text or a new column is seen as it is.

The result is counts-only: column names, statuses and numbers. compare()'s
"issues" text and "new_categories" list contain example values (a new
hospital name, for instance), so they are dropped here, before anything is
written to the report, the log or an alert.
"""

import math

import pandas as pd
from dstoolkit.monitoring import compare

import drift_policy

# Only these fields of compare()'s result leave this module
NUMBERS = ["shift_psi", "p_adjusted", "blanks_before", "blanks_after", "new_category_share"]


def typed(raw: pd.DataFrame) -> pd.DataFrame:
    """Columns whose filled values are all numbers become numbers; the rest stay text.

    Identifier columns stay text even when they hold digits, so they're compared as codes.
    """
    out = raw.copy()
    for col in out.columns:
        if col in drift_policy.IDENTIFIER_COLUMNS:
            continue
        filled = out[col].dropna()
        numbers = pd.to_numeric(filled, errors="coerce")
        if len(filled) and numbers.notna().all():
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("Float64")
    return out


def _clean(value):
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NA:
        return None
    return round(float(value), 4)


def counts_only(result: pd.DataFrame) -> list[dict]:
    """compare()'s rows without any values from the data."""
    rows = []
    for row in result.to_dict("records"):
        rows.append({
            "column": str(row["column"]),
            "status": row["status"],
            "kind": row["kind"],
            **{name: _clean(row[name]) for name in NUMBERS},
            "new_values": len(row["new_categories"]),
            "rows_before": int(row["rows_before"]),
            "rows_after": int(row["rows_after"]),
        })
    return rows


def check(reference: pd.DataFrame, current: pd.DataFrame) -> dict:
    """Drift of current against reference (both from load_raw), with the alert decision."""
    result = compare(typed(reference), typed(current), verbose=False)
    columns, alerts, logged = counts_only(result), [], []
    for row in columns:
        decision, reasons = drift_policy.decide(row)
        row["decision"] = decision
        if decision == drift_policy.ALERT:
            alerts.append({"column": row["column"], "reasons": reasons})
        elif decision == drift_policy.LOG:
            logged.append({"column": row["column"], "reasons": reasons})
    return {
        "status": drift_policy.ALERT if alerts else drift_policy.OK,
        "alerts": alerts,
        "logged": logged,
        "columns": columns,
    }
