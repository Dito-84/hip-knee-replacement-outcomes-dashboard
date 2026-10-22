"""Check a discharge file before building the dashboard from it.

    python scripts/check_data.py private/discharges.xlsx

Prints the dataset-specific checks from outcomes/quality.py. If dstoolkit is
installed (pip install -r requirements-optional.txt), it also writes a full
data profile to private/data_check.html.

Everything stays on your computer. The report contains details of the real
records, so it is only ever written inside private/ (ignored by git) or outside
this repository. Never commit or share it.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from build import SYNTHETIC, _is_safe_output  # noqa: E402
from outcomes.load import DataFormatError, load  # noqa: E402
from outcomes.quality import run_checks  # noqa: E402

# In this file a blank complication, readmission or death field means it didn't happen,
# so the profile must not suggest filling or dropping these columns.
MISSING_OK = ["complication", "during_index_stay", "complication_admit_date", "death_date",
              "readmit_same_hospital", "readmit_provider_id"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("data", type=Path, nargs="?", default=SYNTHETIC, help="CSV or Excel discharge file")
    parser.add_argument("--sheet", help="Excel sheet name")
    parser.add_argument("--out", type=Path, default=ROOT / "private" / "data_check.html",
                        help="where to write the dstoolkit profile (default: private/data_check.html)")
    args = parser.parse_args(argv)

    if not _is_safe_output(args.out.parent):
        print(f"Refusing to write a report of real data to {args.out}.\n"
              f"Use a path inside private/ (ignored by git) or outside this repository.")
        return 2

    try:
        records = load(args.data, sheet=args.sheet)
    except (DataFormatError, FileNotFoundError) as err:
        print(f"Could not read {args.data}: {err}")
        return 1

    print(f"Read {len(records):,} records from {args.data.name}\n")
    findings = run_checks(records)
    if findings:
        for f in findings:
            print(f"  [{f.severity.upper():7}] {f.check}: {f.message}")
    else:
        print("  All dataset checks passed.")

    try:
        import dstoolkit as dst
    except ImportError:
        print("\nFor a full profile of every column, install dstoolkit "
              "(pip install -r requirements-optional.txt) and run this again.")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        try:
            report = dst.profile(records, title=f"Data check: {args.data.name}", missing_ok=MISSING_OK)
        except TypeError:
            print("\nThis dstoolkit is too old for the data check; reinstall it with "
                  "pip install -r requirements-optional.txt")
            return 1
        report.save(args.out)
        print(f"\nFull profile ({len(report.suggestions)} suggestions) written to {args.out}")
        print("It contains details of the records: keep it on this computer.")

    return 1 if any(f.severity == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
