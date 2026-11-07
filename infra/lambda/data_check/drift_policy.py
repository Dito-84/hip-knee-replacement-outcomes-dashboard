"""Which drift findings send an alert. Change the policy here and nowhere else.

dstoolkit's compare() gives each column a status: major, moderate, stable,
added or removed. Alerting on every "major" would email on every upload:
in batches with no real drift, sparse columns (a few dozen filled values)
regularly cross the PSI threshold by chance. So:

- Removed columns, type changes and big jumps in blanks always alert.
  They are exact facts about the file, with no sampling noise.
- A distribution shift or new codes alert only when the status is major
  AND the adjusted p-value is under P_ALERT, i.e. the change is too big
  to be chance at this sample size.
- IDENTIFIER_COLUMNS hold codes that are expected to be new every time
  (most values appear once), so only their type and blanks can alert.
- Everything else that isn't stable (moderate changes, added columns) is
  written to the report and the log, without an alert.
"""

import math

from dstoolkit.monitoring import (
    BLANKS_MAJOR,
    BLANKS_MODERATE,
    NEW_CATEGORIES_MAJOR,
    NEW_CATEGORIES_MODERATE,
    PSI_MAJOR,
    PSI_MODERATE,
)

P_ALERT = 0.01           # adjusted p-value below which a major shift or new codes alert
IDENTIFIER_COLUMNS = {"readmit_provider_id"}

ALERT, LOG, OK = "alert", "log", "ok"


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def decide(row: dict) -> tuple[str, list[str]]:
    """(ALERT, LOG or OK, reasons) for one column of compare()'s result, already made counts-only."""
    status, kind, column = row["status"], str(row["kind"]), row["column"]
    if status == "removed":
        return ALERT, ["column removed"]
    if status == "added":
        return LOG, ["column added"]

    alerts, notes = [], []
    # "date → empty" is a rare-event column with no values in this batch, not a type change
    if "→" in kind and "empty" not in kind:
        alerts.append(f"type changed ({kind})")

    before, after = _number(row["blanks_before"]), _number(row["blanks_after"])
    change = after - before
    if abs(change) >= BLANKS_MAJOR:
        alerts.append(f"blanks {before:.1%} → {after:.1%}")
    elif abs(change) >= BLANKS_MODERATE:
        notes.append(f"blanks {before:.1%} → {after:.1%}")

    if column not in IDENTIFIER_COLUMNS:
        psi, share = _number(row["shift_psi"]), _number(row["new_category_share"])
        psi = 0.0 if math.isnan(psi) else psi
        share = 0.0 if math.isnan(share) else share
        p_adjusted = _number(row["p_adjusted"])
        parts = []
        if psi >= PSI_MODERATE:
            parts.append(f"PSI {psi:.2f}")
        if share >= NEW_CATEGORIES_MODERATE:
            parts.append(f"{int(row['new_values'])} new value(s) in {share:.1%} of rows")
        if parts:
            text = ", ".join(parts) + ("" if math.isnan(p_adjusted) else f", adjusted p {p_adjusted:.2g}")
            # A big change with no p-value (the test couldn't run) is treated as real
            significant = math.isnan(p_adjusted) or p_adjusted < P_ALERT
            if (psi >= PSI_MAJOR or share >= NEW_CATEGORIES_MAJOR) and significant:
                alerts.append(f"distribution shifted ({text})")
            else:
                notes.append(f"possible shift ({text})")

    if alerts:
        return ALERT, alerts
    if notes or status != "stable":
        return LOG, notes or [f"{status} change"]
    return OK, []
