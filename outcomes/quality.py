"""Data-quality checks specific to the discharge-level THA/TKA file.

Run these before building the dashboard from a real export: each check looks
for a problem that would quietly distort the measures (rates, readmissions,
length of stay, age groups) rather than cause an error.

Works on the output of outcomes.load.load(). Needs only pandas.
"""

from dataclasses import dataclass

import pandas as pd

from .metrics import _key


@dataclass
class Finding:
    check: str
    severity: str        # "error" (measures will be wrong) or "warning" (worth a look)
    count: int
    message: str


def run_checks(records: pd.DataFrame) -> list[Finding]:
    """Return one Finding per problem found (an empty list means all checks passed)."""
    r = records
    findings: list[Finding] = []

    def add(check, severity, mask_or_count, message):
        count = int(mask_or_count.sum()) if hasattr(mask_or_count, "sum") else int(mask_or_count)
        if count:
            findings.append(Finding(check, severity, count, message.format(n=count)))

    add("discharge before admission", "error",
        r["discharge_date"] < r["admission_date"],
        "{n} record(s) are discharged before they were admitted; length of stay would be negative.")

    readmit = r["complication_admit_date"]
    add("readmission before discharge", "error",
        readmit.notna() & (readmit < r["discharge_date"]),
        "{n} complication readmission(s) are dated before the index discharge.")

    add("readmission marked during stay", "warning",
        readmit.notna() & r["during_index_stay"],
        "{n} complication(s) are marked as during the index stay but also have a readmission date.")

    age = (r["admission_date"] - r["beneficiary_dob"]).dt.days / 365.25
    add("implausible age", "error", (age < 18) | (age > 110),
        "{n} record(s) have an age at admission under 18 or over 110; check the birth dates.")
    add("missing birth date", "warning", r["beneficiary_dob"].isna(),
        "{n} record(s) have no birth date, so they can't be placed in an age group.")

    add("no procedure", "error", (r["n_tha"] == 0) & (r["n_tka"] == 0),
        "{n} record(s) list neither a hip nor a knee replacement.")

    flagged_without_type = r["had_complication"] & (r["complication"].isna() | (r["complication"] == ""))
    add("complication without type", "warning", flagged_without_type,
        "{n} record(s) say the patient had a complication but don't name it.")

    death_without_date = (r["complication"] == "Death") & r["death_date"].isna()
    add("death without date", "warning", death_without_date,
        "{n} death complication(s) have no death date.")

    # The measures collapse rows to one per stay; extra rows for a stay should be
    # flagged as additional complication records. Unflagged repeats suggest true duplicates.
    key = _key(r)
    repeats = key.duplicated(keep="first")
    add("unflagged repeat rows", "warning", repeats & ~r["additional_record"],
        "{n} row(s) repeat an earlier stay but aren't flagged as additional complication records; "
        "they may be duplicates.")
    add("exact duplicate rows", "error", r.duplicated(),
        "{n} row(s) are exact duplicates of another row.")

    return findings
