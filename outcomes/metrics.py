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

Rates are observed, not the CMS risk-standardized rate. The hospital table
adds an observed/expected ratio adjusted only for age group and procedure.
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
    # A readmission dated before discharge is a data error (outcomes/quality.py flags it),
    # not a readmission on day 0 or earlier, so it never counts toward the 30-day rate.
    comps.loc[comps["readmit_days"] < 0, "readmit_days"] = np.nan

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


def _types_present(c):
    """Measure complication types first (CMS order), then any others in the data."""
    others = sorted(set(c["complication"].dropna()) - set(COMPLICATION_TYPES))
    return COMPLICATION_TYPES + others


def rates_by_type(s, c):
    """Share of surgeries with at least one complication of each type, with 95% CI.

    Per surgery, like every other rate: a stay with two infection records counts once.
    """
    n = len(s)
    stays_with = c.groupby("complication")["stay_key"].nunique()
    rows = []
    for t in _types_present(c):
        k = int(stays_with.get(t, 0))
        low, high = wilson_interval(k, n)
        rows.append({"type": t, "stays": k, "rate": _rate(k, n), "ci": [low, high]})
    return rows


def timing_by_type(c):
    """Complication records by type, split into during the index stay and after discharge."""
    types = _types_present(c)
    during = c[c["during_index_stay"]].groupby("complication").size()
    after = c[~c["during_index_stay"]].groupby("complication").size()
    return {"type": types,
            "during": [int(during.get(t, 0)) for t in types],
            "after": [int(after.get(t, 0)) for t in types]}


LOS_STATUS = ["No complication", "Complication during stay", "Complication after discharge only"]


def los_by_status(s, c):
    """Average length of stay for surgeries with no complication, a complication during
    the index stay, or complications only after discharge."""
    during_keys = set(c.loc[c["during_index_stay"], "stay_key"])
    any_keys = set(c["stay_key"])
    status = np.where(s["stay_key"].isin(during_keys), LOS_STATUS[1],
                      np.where(s["stay_key"].isin(any_keys), LOS_STATUS[2], LOS_STATUS[0]))
    grouped = s.groupby(status)["los_days"]
    means, counts = grouped.mean(), grouped.size()
    return {"status": LOS_STATUS,
            "stays": [int(counts.get(k, 0)) for k in LOS_STATUS],
            "avg_los": [None if k not in means else float(means[k]) for k in LOS_STATUS]}


def odds_ratio(a, b, c, d, z=1.96):
    """Odds ratio for a 2x2 table with a 95% CI (Woolf's log method).

    a, b: exposed with / without the outcome; c, d: unexposed with / without.
    If any cell is zero, 0.5 is added to every cell (Haldane-Anscombe correction).
    """
    if min(a + b, c + d) == 0 or min(a + c, b + d) == 0:
        return None, None, None
    if 0 in (a, b, c, d):
        a, b, c, d = a + 0.5, b + 0.5, c + 0.5, d + 0.5
    ratio = (a * d) / (b * c)
    se = np.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
    return float(ratio), float(np.exp(np.log(ratio) - z * se)), float(np.exp(np.log(ratio) + z * se))


def hip_vs_knee_odds(stays, comps, hospital="All", population="all"):
    """Odds of each complication after a hip (THA) vs a knee (TKA) replacement.

    Unadjusted. Stays with both procedures are left out, since they belong to
    neither group. Ignores the procedure filter by design.
    """
    s = filter_stays(stays, hospital, "All", population)
    s = s[s["has_tha"] != s["has_tka"]]
    c = comps[comps["stay_key"].isin(s["stay_key"])]
    if population == "adjusted":
        c = c[~c["covid_excluded"]]
    hip = set(s.loc[s["has_tha"], "stay_key"])
    n_hip, n_knee = len(hip), len(s) - len(hip)
    rows = []
    for t in ["Any complication"] + _types_present(c):
        keys = set(c["stay_key"]) if t == "Any complication" else set(c.loc[c["complication"] == t, "stay_key"])
        a = len(keys & hip)
        k = len(keys) - a
        ratio, low, high = odds_ratio(a, n_hip - a, k, n_knee - k)
        rows.append({"type": t, "hip": a, "hip_n": n_hip, "knee": k, "knee_n": n_knee,
                     "odds_ratio": ratio, "ci": [low, high]})
    return rows


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
    timing = timing_by_type(c)
    order = [timing["type"].index(t) for t in type_counts.index]
    timing = {key: [vals[i] for i in order] for key, vals in timing.items()}

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
        "timing_by_type": timing,
        "rate_by_type": rates_by_type(s, c),
        "los_by_status": los_by_status(s, c),
        "hip_vs_knee": hip_vs_knee_odds(stays, comps, hospital, population),
        "los": {"days": LOS_LABELS, "stays": los.astype(int).tolist()},
        "readmit_days": {"bucket": READMIT_LABELS, "stays": readmit_hist.fillna(0).astype(int).tolist()},
        "by_age": {
            "group": AGE_LABELS,
            "stays": by_age["stays"].astype(int).tolist(),
            "complication_rate": [_rate(c_, n) for c_, n in zip(by_age["complications"], by_age["stays"])],
        },
    }


def _procedure_group(stays):
    return np.select([stays["has_tha"] & stays["has_tka"], stays["has_tha"]], ["Both", "THA"], "TKA")


def expected_complications(reference, target, population):
    """Indirect standardization by age group and procedure.

    Each stratum's complication rate in `reference` is applied to the stays in
    `target`; the sum is the number of complications `target` would have had
    at the reference rates, given its own patient mix.
    """
    flag = f"complication_{population}"

    def strata(s):
        return s["age_group"].astype(str) + "|" + pd.Series(_procedure_group(s), index=s.index)

    rates = reference.groupby(strata(reference))[flag].mean()
    return float(strata(target).map(rates).fillna(reference[flag].mean()).sum())


def poisson_ratio_interval(observed, expected, z=1.96):
    """95% interval for observed/expected, treating observed as Poisson (Byar's approximation)."""
    if expected == 0:
        return None, None
    o = observed
    low = 0.0 if o == 0 else o * (1 - 1 / (9 * o) - z / (3 * np.sqrt(o))) ** 3
    high = (o + 1) * (1 - 1 / (9 * (o + 1)) + z / (3 * np.sqrt(o + 1))) ** 3
    return float(low / expected), float(high / expected)


def hospital_table(stays, procedure="All", population="all"):
    """Side-by-side comparison of hospitals (ignores the hospital filter).

    Besides observed rates, each hospital gets an observed/expected (O/E) ratio
    adjusted for age group and procedure, against all hospitals combined.
    This is not the CMS risk-standardized rate (see docs/measure-crosswalk.md).
    """
    reference = filter_stays(stays, "All", procedure, population)
    ref_rate = _rate(reference[f"complication_{population}"].sum(), len(reference))
    rows = []
    for hospital in sorted(stays["hospital"].dropna().unique()):
        s = filter_stays(stays, hospital, procedure, population)
        k = kpis(s, population)
        low, high = wilson_interval(k["complications"], k["stays"])
        expected = expected_complications(reference, s, population) if len(s) else 0.0
        oe = _rate(k["complications"], expected)
        oe_low, oe_high = poisson_ratio_interval(k["complications"], expected)
        rows.append({"hospital": hospital, **k, "complication_ci": [low, high],
                     "expected": expected, "oe_ratio": oe, "oe_ci": [oe_low, oe_high],
                     "adjusted_rate": None if oe is None or ref_rate is None else oe * ref_rate})
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
