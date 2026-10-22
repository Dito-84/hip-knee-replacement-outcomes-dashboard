"""Column definitions for the discharge-level THA/TKA complication file.

Each column has a short internal name and the header used in the source
spreadsheet. Real exports sometimes break headers across lines
("Inclusion/\\nExclusion Indicator") or add hints in parentheses, so headers
are matched on a normalized key: lowercase letters and digits only, and a
source header matches if its key starts with the expected key.
"""

import re

# internal name -> (source header, required?)
COLUMNS = {
    "hospital": ("Hospital", True),
    "measure": ("Measure", False),
    "beneficiary_dob": ("Beneficiary DOB", True),
    "admission_date": ("Admission Date of Index Stay", True),
    "discharge_date": ("Discharge Date of Index Stay", True),
    "additional_record": ("Additional Complication Record (Yes/No)", False),
    "covid_excluded": ("Complication Excluded Due to COVID-19 (Yes/No)", False),
    "inclusion_exclusion": ("Inclusion/Exclusion Indicator", False),
    "n_tka": ("Number of TKAs Performed (0, 1, or 2)", True),
    "n_tha": ("Number of THAs Performed (0, 1, or 2)", True),
    "had_complication": ("Patient Had a Complication (Yes/No)", True),
    "excluded": ("Excluded (Yes/No)", True),
    "complication": ("Complication", True),
    "during_index_stay": ("Complication Occurred During Index Stay (Yes/No)", False),
    "complication_admit_date": ("Admit Date for Complication (If Not During Index Stay)", False),
    "death_date": ("Death Date", False),
    "readmit_same_hospital": ("Readmission to Same Hospital (Yes/No)", False),
    "readmit_provider_id": ("Provider ID of Readmitting Hospital", False),
}

DATE_COLUMNS = ["beneficiary_dob", "admission_date", "discharge_date", "complication_admit_date", "death_date"]
YES_NO_COLUMNS = ["additional_record", "covid_excluded", "had_complication", "excluded",
                  "during_index_stay", "readmit_same_hospital"]
COUNT_COLUMNS = ["n_tka", "n_tha"]

# Complication types in the CMS THA/TKA complication measure
COMPLICATION_TYPES = [
    "AMI",
    "Pneumonia",
    "Sepsis",
    "Surgical site bleeding",
    "Pulmonary embolism",
    "Death",
    "Mechanical complication",
    "Infection",
]


def header_key(header):
    """'Inclusion/\\nExclusion Indicator' -> 'inclusionexclusionindicator'."""
    return re.sub(r"[^a-z0-9]", "", str(header).lower())


# Keys are matched longest-first so "complication" doesn't swallow
# "complicationexcludedduetocovid19" and similar longer headers.
_EXPECTED = sorted(
    ((name, header_key(src.split("(")[0])) for name, (src, _) in COLUMNS.items()),
    key=lambda item: -len(item[1]),
)


def match_headers(headers):
    """Map source headers to internal names.

    Returns (mapping, missing_required) where mapping is {source_header: name}.
    """
    mapping, used = {}, set()
    for header in headers:
        # Ignore parenthetical hints like "(Yes/No)" or "(0, 1, or 2)"
        key = header_key(re.sub(r"\(.*?\)", "", str(header), flags=re.S))
        for name, expected in _EXPECTED:
            if name in used:
                continue
            exact_name_only = name in ("complication", "hospital", "measure", "excluded")
            if (key == expected) if exact_name_only else key.startswith(expected):
                mapping[header] = name
                used.add(name)
                break
    missing = [name for name, (_, required) in COLUMNS.items() if required and name not in used]
    return mapping, missing
