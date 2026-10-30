# Glossary

Plain-language definitions of the terms used on the dashboard. For how each measure is calculated and how it relates to the CMS definition, see the [measure crosswalk](measure-crosswalk.md).

## Surgeries and populations

**THA / TKA:** total hip arthroplasty and total knee arthroplasty, the hip and knee replacement procedures the CMS complication measure covers.

**Index admission (surgery):** the hospital stay in which a THA, a TKA or both were performed. It's the unit every rate is counted in: one index admission is one surgery, however many complication records it has.

**Hip / knee surgeries:** index admissions in which at least one THA (or TKA) was performed. A stay with both procedures counts under both filters, and once under "All".

**Excluded:** a stay that doesn't meet the CMS criteria for inclusion in the measure, as flagged in the source data.

**Qualifying index admission:** an index admission that isn't excluded.

**COVID-19-excluded complication:** a complication flagged in the source data as excluded because of COVID-19. The stay still counts; that complication doesn't.

**All surgeries / exclusion-adjusted:** the two populations on the dashboard. "All surgeries" counts every index admission and every complication. "Exclusion-adjusted" counts only qualifying index admissions, and leaves out COVID-19-excluded complications.

## Complications

**Complication:** one of the eight outcomes in the CMS measure, within its follow-up window:
- within 7 days of admission: acute myocardial infarction (AMI), pneumonia, sepsis
- within 30 days: surgical site bleeding, pulmonary embolism, death
- within 90 days: mechanical complication, infection (periprosthetic joint or wound)

**Complication record:** one row of the source data. A surgery with two complications has two records, the second flagged as an additional record.

**During the stay / after discharge:** whether the complication happened before the patient left the hospital or after discharge. A complication after discharge comes with a readmission date.

**Complication rate:** the share of surgeries with at least one complication. Each surgery counts once, however many complications it had.

**Complication rate by type:** the share of surgeries with at least one complication of a given type, such as infection.

**Mortality rate:** the share of surgeries where the patient died within 30 days, one of the measure's complications.

## Readmissions and time

**Complication readmission:** a return to hospital for one of the measure's complications. The data doesn't record readmissions for other reasons, so this isn't the CMS all-cause readmission rate.

**Days from discharge to readmission:** the readmission date minus the index stay's discharge date. When a surgery has several readmissions, the first one counts.

**30-day complication readmission rate:** the share of surgeries readmitted for a complication within 30 days of discharge. Day 30 counts; day 31 doesn't.

**Readmission to the same hospital:** whether the patient was readmitted to the hospital where the surgery happened. It's in the data, but not shown on the dashboard.

**Length of stay:** the discharge date minus the admission date, in days. Same-day discharge is 0.

**Age:** the patient's age in whole years on the admission date. Age groups are under 65, then 65–69 to 80–84 in five-year bands, then 85 and over.

## Comparisons

**95% confidence interval:** the range a rate would likely fall in if the same hospital and period were repeated. It's wide when a group has few surgeries. Rates use the Wilson interval.

**Observed rate:** complications ÷ surgeries, with no adjustment for how sick or old the patients were. Every rate on the dashboard is observed.

**Risk-standardized complication rate (RSCR):** the rate CMS publishes. It adjusts for patient risk factors drawn from a year of earlier Medicare claims. This data doesn't have those, so the dashboard doesn't calculate it.

**Observed vs expected (O/E):** a hospital's actual complications ÷ the number expected if its patients had the complication rates of all hospitals combined, for their age group and procedure. 1.0 means as expected; above 1.0 means more than expected. It adjusts for age and procedure only.

**Odds ratio (hip vs knee):** the odds of a complication after a hip replacement ÷ the odds after a knee replacement. Odds are complications ÷ no complications. Above 1 means higher odds after a hip replacement. Surgeries with both procedures are left out. The unadjusted interval is Woolf's method; when a cell of the 2×2 table is zero, 0.5 is added to every cell.

**Age-adjusted odds ratio:** the same comparison made within each age group, then pooled (the Mantel-Haenszel method), so a difference in age mix between hip and knee patients can't create or hide a difference.

**Small-cell suppression:** hiding any count of 1 to 10 people, and anything computed from one, before a build is shared. CMS doesn't allow publishing smaller counts. Hidden values show as "–".
