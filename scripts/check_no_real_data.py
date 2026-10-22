"""Fail if anything that could contain real patient data is tracked by git.

Runs in CI on every push and pull request. Allowed data: the synthetic CSV
in data/ (which must match what the generator produces; see tests).
"""

import subprocess
import sys
from pathlib import PurePosixPath

ALLOWED_DATA = {"data/synthetic_discharges.csv"}
BLOCKED_SUFFIXES = {".xlsx", ".xls", ".xlsm", ".qvf", ".qvd", ".sav", ".sas7bdat", ".parquet", ".feather"}
DATA_SUFFIXES = {".csv", ".tsv", ".json"}


def main():
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    problems = []
    for f in files:
        path = PurePosixPath(f)
        if path.parts and path.parts[0] == "private":
            problems.append(f"{f}: files in private/ must never be committed")
        elif path.suffix.lower() in BLOCKED_SUFFIXES:
            problems.append(f"{f}: spreadsheet/data exports are not allowed in this repo")
        elif path.suffix.lower() in DATA_SUFFIXES and path.parts[0] == "data" and f not in ALLOWED_DATA:
            problems.append(f"{f}: only the synthetic dataset may live in data/")
    if problems:
        print("Possible real data found in the repository:\n  " + "\n  ".join(problems))
        sys.exit(1)
    print(f"OK: {len(files)} tracked files, no real-data files")


if __name__ == "__main__":
    main()
