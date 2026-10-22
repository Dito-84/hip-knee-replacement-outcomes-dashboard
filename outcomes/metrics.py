"""Measures for the THA/TKA outcomes dashboard.

Two corrections compared with the original Qlik version:

1. **Rates are per surgery, not per row.** A patient with two complications
   appears on two rows (the second flagged as an additional record). The
   Qlik measures counted rows, so those patients inflated the denominator
   and every rate came out slightly low. Here rows are first collapsed to one
   row per index stay.
2. **"Readmissions" are complication readmissions.** The only readmission
   date in the file is the admit date for a complication, so the measure is
   labelled "30-day complication readmission", not all-cause readmission.

Exclusion-adjusted views drop stays excluded from the CMS measure, and
complications flagged as excluded due to COVID-19.
"""

import numpy as np
import pandas as pd

from .schema import COMPLICATION_TYPES

AGE_BINS = [0, 65, 70, 75, 80, 85, 200]
AGE_LABELS = ["Under 65", "65–69", "70–74", "75–79", "80–84", "85+"]
LOS_LABELS = ["0", "1", "2", "3", "4", "5+"]
READMIT_BINS = [-1, 7, 14, 30, 60, 90, 10_000]
READMIT_LABELS = ["0–7", "8–14", "15–30", "31–60", "61–90", "90+"]

PROCEDURES = ["All", "THA", "TKA"]
POPULATIONS = ["all", "adjusted"]


def _key(df):
    """One key per index stay: same hospital, patient birth date and stay dates."""
    parts = [df["hospital"].astype(str)]
    for col in ("beneficiary_dob", "admission_date", "discharge_date"):
        parts.append(df[col].dt.strftime("%Y-%m-%d").fillna("?"))
    key = parts[0]
    for p in parts[1:]:
        key = key + "|" + p
    return key


def build_stays(records):
    """Collapse complication records into one row per index stay.

    Returns (stays, complications):
      stays: one row per surgery with outcome flags for both populations
      complications: one row per counted complication record
    """
    df = records.copy()
    df["stay_key"] = _key(df)

    is_comp = df["had_complication"] & df["complication"].notna() & (df["complication"] != "")
    comps = df.loc[is_comp, ["stay_key", "complication", "during_index_stay",
                             "complication_admit_date", "discharge_date", "covid_excluded"]].copy()
    comps["readmit_days"] = (comps["complication_admit_date"] - comps["discharge_date"]).dt.days
    comps.loc[comps["during_index_stay"], "readmit_days"] = np.nan

    stays = df.groupby("stay_key", sort=False).agg(
        hospital=("hospital", "first"),
        admission_date=("admission_date", "first"),
        discharge_date=("discharge_date", "first"),
        beneficiary_dob=("beneficiary_dob", "first"),
        n_tha=("n_tha", "max"),
        n_tka=("n_tka", "max"),
        excluded=("excluded", "max"),
    )
    stays["has_tha"] = stays["n_tha"] > 0
    stays["has_tka"] = stays["n_tka"] > 0
    stays["year"] = stays["admission_date"].dt.year
    stays["los_days"] = (stays["discharge_date"] - stays["admission_date"]).dt.days
    age = stays["admission_date"].dt.year - stays["beneficiary_dob"].dt.year
    had_birthday = (stays["admission_date"].dt.month > stays["beneficiary_dob"].dt.month) | (
        (stays["admission_date"].dt.month == stays["beneficiary_dob"].dt.month)
        & (stays["admission_date"].dt.day >= stays["beneficiary_dob"].dt.day))
    stays["age"] = age - (~had_birthday).astype(int)
    stays["age_group"] = pd.cut(stays["age"], AGE_BINS, right=False, labels=AGE_LABELS)

    # Outcome flags, computed twice: all complications, and excluding COVID-flagged ones
    for suffix, subset in (("all", comps), ("adjusted", comps[~comps["covid_excluded"]])):
        grouped = subset.groupby("stay_key")
        stays[f"complication_{suffix}"] = stays.index.isin(subset["stay_key"])
        stays[f"death_{suffix}"] = stays.index.isin(subset.loc[subset["complication"] == "Death", "stay_key"])
        first_readmit = grouped["readmit_days"].min()
        stays[f"readmit_days_{suffix}"] = first_readmit.reindex(stays.index)

    return stays.reset_index(), comps


def filter_stays(stays, hospital="All", procedure="All", population="all"):
    s = stays
    if hospital != "All":
        s = s[s["hospital"] == hospital]
    if procedure == "THA":
        s = s[s["has_tha"]]
    elif procedure == "TKA":
        s = s[s["has_tka"]]
    if population == "adjusted":
        s = s[~s["excluded"]]
    return s


def _rate(numerator, denominator):
    return None if denominator == 0 else float(numerator) / float(denominator)


def _mean(series):
    series = series.dropna()
    return None if series.empty else float(series.mean())


def wilson_interval(successes, n, z=1.96):
    """95% confidence interval for a proportion (stable for small counts)."""
    if n == 0:
        return None, None
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return float(max(0.0, center - half)), float(min(1.0, center + half))


def kpis(s, population):
    n = len(s)
    readmit = s[f"readmit_days_{population}"]
    return {
        "stays": n,
        "complications": int(s[f"complication_{population}"].sum()),
        "complication_rate": _rate(s[f"complication_{population}"].sum(), n),
        "readmit30": int((readmit <= 30).sum()),
        "readmit30_rate": _rate((readmit <= 30).sum(), n),
        "deaths": int(s[f"death_{population}"].sum()),
        "mortality_rate": _rate(s[f"death_{population}"].sum(), n),
        "avg_los": _mean(s["los_days"]),
        "avg_age": _mean(s["age"]),
    }


def summarize(stays, comps, hospital="All", procedure="All", population="all"):
    """Everything the dashboard shows for one combination of filters."""
    s = filter_stays(stays, hospital, procedure, population)
    c = comps[comps["stay_key"].isin(s["stay_key"])]
    if population == "adjusted":
        c = c[~c["covid_excluded"]]
    flag = f"complication_{population}"

    by_year = s.groupby("year").agg(stays=("stay_key", "size"), complications=(flag, "sum"))
    by_age = s.groupby("age_group", observed=False).agg(stays=("stay_key", "size"), complications=(flag, "sum"))
    type_counts = c["complication"].value_counts().reindex(COMPLICATION_TYPES, fill_value=0)
    other = c.loc[~c["complication"].isin(COMPLICATION_TYPES), "complication"].value_counts()
    type_counts = pd.concat([type_counts, other]).sort_values(ascending=False)

    los = s["los_days"].clip(upper=5).value_counts().reindex(range(6), fill_value=0)
    readmit_days = s[f"readmit_days_{population}"].dropna()
    readmit_hist = pd.cut(readmit_days, READMIT_BINS, labels=READMIT_LABELS).value_counts().reindex(READMIT_LABELS)

    return {
        "kpis": kpis(s, population),
        "by_year": {
            "year": [int(y) for y in by_year.index],
            "stays": by_year["stays"].astype(int).tolist(),
            "complication_rate": [_rate(c_, n) for c_, n in zip(by_year["complications"], by_year["stays"])],
        },
        "by_type": {"type": type_counts.index.tolist(), "count": type_counts.astype(int).tolist()},
        "los": {"days": LOS_LABELS, "stays": los.astype(int).tolist()},
        "readmit_days": {"bucket": READMIT_LABELS, "stays": readmit_hist.fillna(0).astype(int).tolist()},
        "by_age": {
            "group": AGE_LABELS,
            "stays": by_age["stays"].astype(int).tolist(),
            "complication_rate": [_rate(c_, n) for c_, n in zip(by_age["complications"], by_age["stays"])],
        },
    }


def hospital_table(stays, procedure="All", population="all"):
    """Side-by-side comparison of hospitals (ignores the hospital filter)."""
    rows = []
    for hospital in sorted(stays["hospital"].dropna().unique()):
        s = filter_stays(stays, hospital, procedure, population)
        k = kpis(s, population)
        low, high = wilson_interval(k["complications"], k["stays"])
        rows.append({"hospital": hospital, **k, "complication_ci": [low, high]})
    return rows


def build_dashboard_data(records, source_label):
    stays, comps = build_stays(records)
    hospitals = ["All"] + sorted(stays["hospital"].dropna().unique().tolist())
    views = {}
    for population in POPULATIONS:
        for procedure in PROCEDURES:
            for hospital in hospitals:
                views[f"{hospital}|{procedure}|{population}"] = summarize(stays, comps, hospital, procedure, population)
            views[f"__hospitals__|{procedure}|{population}"] = hospital_table(stays, procedure, population)
    return {
        "source": source_label,
        "hospitals": hospitals,
        "date_range": [stays["admission_date"].min().strftime("%b %Y"), stays["admission_date"].max().strftime("%b %Y")],
        "records": int(len(records)),
        "views": views,
    }
