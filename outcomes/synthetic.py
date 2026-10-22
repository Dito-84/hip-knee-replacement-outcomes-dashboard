"""Generate a synthetic discharge-level THA/TKA complication dataset.

Every value is randomly generated: no row corresponds to a real patient.
Volumes and rates are set from published national patterns for Medicare
hip and knee replacement (complication rate around 2.5%, typical length of
stay falling from about 2 days in 2018 toward 1 day by 2021, an elective
surgery pause in spring 2020) so the dashboard behaves realistically.

Usage:
    python -m outcomes.synthetic            # writes data/synthetic_discharges.csv
"""

from pathlib import Path

import numpy as np
import pandas as pd

from .schema import COLUMNS

SEED = 2023
START, END = pd.Timestamp("2018-01-01"), pd.Timestamp("2021-12-31")

# Relative size and complication-risk multiplier for each fictional hospital
HOSPITALS = {"Hospital A": (0.16, 1.20), "Hospital B": (0.44, 0.92), "Hospital C": (0.40, 1.02)}
TOTAL_STAYS = 4000
BASE_COMPLICATION_RATE = 0.024

# Share of complications by type, chance it happens during the index stay,
# and the window (days after discharge) the measure counts it in
COMPLICATIONS = {
    #                          share  during_stay  window
    "Mechanical complication": (0.28, 0.05, 90),
    "Infection":               (0.24, 0.02, 90),
    "Pulmonary embolism":      (0.12, 0.25, 30),
    "Pneumonia":               (0.10, 0.40, 7),
    "Sepsis":                  (0.07, 0.30, 7),
    "Death":                   (0.07, 0.10, 30),
    "AMI":                     (0.06, 0.60, 7),
    "Surgical site bleeding":  (0.06, 0.50, 30),
}

EXCLUSION_REASONS = [
    "Excluded: transferred in from another facility",
    "Excluded: hip fracture",
    "Excluded: revision or partial procedure",
    "Excluded: not enrolled in Medicare 12 months prior",
    "Excluded: discharged against medical advice",
]


def _monthly_weights(months):
    """Relative surgical volume per month, with the spring 2020 elective pause."""
    weights = []
    for m in months:
        w = 1.0
        if m.year == 2020 and m.month in (4, 5):
            w = 0.12
        elif m.year == 2020 and m.month in (3, 6):
            w = 0.55
        elif m.year >= 2020:
            w = 0.88
        weights.append(w)
    w = np.array(weights)
    return w / w.sum()


def _yes_no(flag):
    return "Yes" if flag else "No"


def generate(n_stays=TOTAL_STAYS, seed=SEED):
    rng = np.random.default_rng(seed)
    months = pd.date_range(START, END, freq="MS")
    month_p = _monthly_weights(months)
    names = list(HOSPITALS)
    hosp_p = np.array([HOSPITALS[h][0] for h in names])
    hosp_p = hosp_p / hosp_p.sum()
    types = list(COMPLICATIONS)
    type_p = np.array([COMPLICATIONS[t][0] for t in types])
    type_p = type_p / type_p.sum()

    rows, seen_keys = [], set()
    while len(seen_keys) < n_stays:
        hospital = names[rng.choice(len(names), p=hosp_p)]
        month = months[rng.choice(len(months), p=month_p)]
        admit = month + pd.Timedelta(days=int(rng.integers(0, month.days_in_month)))

        # Age: mostly Medicare 65+, a small share under 65 (disability)
        if rng.random() < 0.07:
            age = int(rng.integers(50, 65))
        else:
            age = int(np.clip(rng.normal(73, 6.5), 65, 96))
        dob = admit - pd.DateOffset(years=age) - pd.Timedelta(days=int(rng.integers(1, 365)))

        key = (hospital, dob, admit)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        # Procedure: knees are more common; a few bilateral or combined cases
        r = rng.random()
        if r < 0.59:
            n_tka, n_tha = (2 if rng.random() < 0.04 else 1), 0
        elif r < 0.995:
            n_tka, n_tha = 0, (2 if rng.random() < 0.01 else 1)
        else:
            n_tka, n_tha = 1, 1

        # Length of stay shortens each year; older patients stay longer
        mean_los = {2018: 2.2, 2019: 1.9, 2020: 1.6, 2021: 1.4}[admit.year]
        mean_los += 0.04 * max(0, age - 75) + 0.5 * (n_tka + n_tha - 1)
        los = int(min(rng.poisson(mean_los), 14))
        if admit.year >= 2020 and rng.random() < 0.08:
            los = 0  # same-day discharge
        discharge = admit + pd.Timedelta(days=los)

        excluded = rng.random() < 0.10
        inclusion = rng.choice(EXCLUSION_REASONS) if excluded else "Included"

        risk = BASE_COMPLICATION_RATE * HOSPITALS[hospital][1] * (1 + 0.035 * max(0, age - 70))
        if n_tka + n_tha > 1:
            risk *= 1.6

        base = {
            "hospital": hospital,
            "measure": "THA/TKA Complication",
            "beneficiary_dob": dob,
            "admission_date": admit,
            "discharge_date": discharge,
            "inclusion_exclusion": inclusion,
            "n_tka": n_tka,
            "n_tha": n_tha,
            "excluded": _yes_no(excluded),
        }

        if rng.random() >= risk:
            rows.append({**base, "had_complication": "No", "additional_record": "No",
                         "covid_excluded": "No", "complication": ""})
            continue

        # One complication, sometimes a second on an additional record
        n_records = 2 if rng.random() < 0.12 else 1
        chosen = rng.choice(len(types), size=n_records, replace=False, p=type_p)
        readmit_date = None
        for i, t_idx in enumerate(chosen):
            ctype = types[t_idx]
            _, p_during, window = COMPLICATIONS[ctype]
            during = rng.random() < p_during
            row = {**base, "had_complication": "Yes", "additional_record": _yes_no(i > 0),
                   "complication": ctype, "during_index_stay": _yes_no(during)}

            covid_period = admit >= pd.Timestamp("2020-04-01")
            row["covid_excluded"] = _yes_no(
                covid_period and ctype in ("Pneumonia", "Sepsis", "Death") and rng.random() < 0.25)

            if ctype == "Death":
                days = 0 if during else int(rng.integers(1, window + 1))
                row["death_date"] = discharge + pd.Timedelta(days=days)
            elif not during:
                if readmit_date is None:
                    # Most readmissions happen early in the window
                    days = int(np.clip(rng.gamma(1.6, window / 4), 1, window))
                    readmit_date = discharge + pd.Timedelta(days=days)
                row["complication_admit_date"] = readmit_date
                same = rng.random() < 0.82
                row["readmit_same_hospital"] = _yes_no(same)
                row["readmit_provider_id"] = "" if same else f"10{rng.integers(1000, 9999)}"
            rows.append(row)

    df = pd.DataFrame(rows)
    # Order and name columns exactly like the source spreadsheet
    df = df.reindex(columns=list(COLUMNS))
    for col in ("beneficiary_dob", "admission_date", "discharge_date", "complication_admit_date", "death_date"):
        df[col] = pd.to_datetime(df[col]).dt.strftime("%m/%d/%Y")
    df = df.fillna("").sort_values("admission_date", key=pd.to_datetime, kind="stable")
    return df.rename(columns={name: src for name, (src, _) in COLUMNS.items()})


def main():
    out = Path(__file__).resolve().parent.parent / "data" / "synthetic_discharges.csv"
    out.parent.mkdir(exist_ok=True)
    df = generate()
    df.to_csv(out, index=False)
    print(f"Wrote {len(df):,} rows to {out}")


if __name__ == "__main__":
    main()
