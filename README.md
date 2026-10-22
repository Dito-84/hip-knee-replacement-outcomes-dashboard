# Hip & Knee Replacement Outcomes Dashboard

[![CI](https://github.com/Dito-84/hip-knee-replacement-outcomes-dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/Dito-84/hip-knee-replacement-outcomes-dashboard/actions/workflows/ci.yml)

An interactive dashboard of complications, complication readmissions and length of stay after **total hip arthroplasty (THA)** and **total knee arthroplasty (TKA)**, built on the CMS risk-standardized complication measure definitions.

**[Open the live dashboard →](https://dito-84.github.io/hip-knee-replacement-outcomes-dashboard/)**

> **All data in this repository is synthetic.** The dashboard was originally built in Qlik Sense on de-identified hospital discharge data, which is not included here and never leaves the analyst's computer. This version is rebuilt in Python and runs on a randomly generated dataset with the same structure, so it can be shared publicly.

![Dashboard screenshot](docs/dashboard.png)

## What it shows

- **Headline measures:** surgeries, complication rate, 30-day complication readmission rate, mortality and average length of stay
- **Trends by year,** including the spring 2020 drop in elective surgery
- **Complications by type:** the eight complications in the CMS measure (mechanical complications, infection, pulmonary embolism, pneumonia, sepsis, AMI, surgical site bleeding, death)
- **Hospital comparison** with 95% confidence intervals, so small hospitals aren't over-read
- **Complication rate by age group,** length of stay and time from discharge to readmission

Filter by hospital, by procedure (hip or knee), and by population: all surgeries, or exclusion-adjusted (removing stays excluded from the CMS measure and complications flagged as COVID-19 related).

## How the measures are counted

Rebuilding the dashboard fixed two issues in the original Qlik version:

1. **Rates are per surgery, not per row.** A patient with two complications appears on two records. The original measures counted records, which inflated the denominator and understated every rate. Records are now collapsed to one row per index stay first ([`outcomes/metrics.py`](outcomes/metrics.py)).
2. **Readmissions are labelled correctly.** The data only records readmissions *for a complication*, so the measure is "30-day complication readmission", not the CMS all-cause readmission rate.

## Project structure

| Path | Purpose |
|---|---|
| `outcomes/schema.py` | Column definitions; matches the original export's headers even with line breaks |
| `outcomes/load.py` | Reads CSV or Excel, finds the header row, cleans dates and Yes/No fields |
| `outcomes/metrics.py` | Collapses records to stays and computes every measure for each filter combination |
| `outcomes/synthetic.py` | Generates the synthetic dataset (seeded, reproducible) |
| `dashboard/template.html` | The dashboard page (Plotly.js), filled with precomputed data at build time |
| `build.py` | Builds a single self-contained `index.html` |
| `tests/` | pytest suite: header matching, stay-level counting, 30-day window, filters, synthetic data |
| `scripts/check_no_real_data.py` | CI check that fails if any spreadsheet or non-synthetic data file is committed |

## Run it locally

```bash
pip install -r requirements.txt
python build.py            # builds _site/index.html from the synthetic data
open _site/index.html
```

Run the tests with `pip install pytest && pytest`. Regenerate the synthetic data with `python -m outcomes.synthetic`.

## Using your own data

The loader accepts the original discharge-level export (CSV or Excel) with the standard columns: Hospital, Beneficiary DOB, Admission/Discharge Date of Index Stay, Number of THAs/TKAs Performed, Patient Had a Complication, Excluded, Complication, and so on. Keep real data in the `private/` folder, which git ignores:

```bash
mkdir private
cp ~/Downloads/<your file>.xlsx private/
python build.py --data private/<your file>.xlsx --out private/site
open private/site/index.html
```

`build.py` refuses to write a build of real data anywhere except `private/` or outside the repository, and CI rejects any committed spreadsheet, so real results can't be published by accident. **Never commit patient data or screenshots of it.**

## Synthetic data

`outcomes/synthetic.py` generates 4,000 fictional surgeries across three fictional hospitals (2018–2021). Volumes and rates follow published national patterns for Medicare hip and knee replacement: a complication rate around 3%, length of stay falling each year, and the 2020 elective surgery pause. No values are derived from real patient records. Year-to-year swings in the charts are random noise, not findings.

## Built with

Python (pandas, NumPy), Plotly.js, pytest, GitHub Actions and GitHub Pages.
