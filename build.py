"""Build the dashboard as a single self-contained HTML page.

Synthetic data (the public version):
    python build.py                       # -> _site/index.html

Your own data, kept on your computer:
    python build.py --data private/discharges.xlsx --out private/site

A build from any file other than the synthetic dataset must be written
inside private/ (which git ignores) or outside this repository, so real
results can't be committed by accident.

To share a build, hide small counts (CMS doesn't allow publishing counts of 1 to 10):
    python build.py --data private/discharges.xlsx --out private/site --suppress-under 11

Every build runs the data-quality checks in outcomes/quality.py first and
stops if one finds an error that would make the measures wrong. Use
--skip-checks only after reviewing the findings with scripts/check_data.py.
"""

import argparse
import json
import sys
from pathlib import Path

from outcomes.load import DataFormatError, load
from outcomes.metrics import build_dashboard_data
from outcomes.quality import run_checks
from outcomes.suppression import suppress

ROOT = Path(__file__).resolve().parent
SYNTHETIC = ROOT / "data" / "synthetic_discharges.csv"
TEMPLATE = ROOT / "dashboard" / "template.html"
PLACEHOLDER = "/*__DATA__*/null"


def _is_safe_output(out_dir):
    out_dir = out_dir.resolve()
    private = (ROOT / "private").resolve()
    return private in out_dir.parents or out_dir == private or ROOT not in out_dir.parents


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=SYNTHETIC, help="CSV or Excel discharge file")
    parser.add_argument("--sheet", help="Excel sheet name (default: first sheet with the expected headers)")
    parser.add_argument("--out", type=Path, default=ROOT / "_site", help="output folder")
    parser.add_argument("--label", help="description shown in the page banner for non-synthetic data")
    parser.add_argument("--suppress-under", type=int, default=0, metavar="N",
                        help="hide counts of 1 to N-1 and figures computed from them; use 11 for any build "
                             "meant to be shared (CMS cell-size rule). Default: off")
    parser.add_argument("--skip-checks", action="store_true",
                        help="build even if a data-quality check finds an error (not recommended)")
    args = parser.parse_args(argv)

    synthetic = args.data.resolve() == SYNTHETIC.resolve()
    if not synthetic and not _is_safe_output(args.out):
        sys.exit(f"Refusing to write a build of real data to {args.out}.\n"
                 f"Use --out private/site (ignored by git) or a folder outside this repository.")

    try:
        records = load(args.data, sheet=args.sheet)
    except (DataFormatError, FileNotFoundError) as err:
        sys.exit(f"Could not read {args.data}: {err}")

    findings = run_checks(records)
    for f in findings:
        print(f"{f.severity.upper()}: {f.message}", file=sys.stderr)
    errors = [f for f in findings if f.severity == "error"]
    if errors and not args.skip_checks:
        sys.exit(f"Stopped: {len(errors)} data-quality check(s) found errors that would make the measures wrong.\n"
                 f"Fix the file, or review it with scripts/check_data.py and rerun with --skip-checks.")

    label = "synthetic" if synthetic else (args.label or f"{args.data.name}")
    data = build_dashboard_data(records, label)
    if args.suppress_under > 1:
        data = suppress(data, args.suppress_under)

    html = TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in html:
        sys.exit("Template is missing the data placeholder")
    payload = json.dumps(data, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    html = html.replace(PLACEHOLDER, payload)

    args.out.mkdir(parents=True, exist_ok=True)
    out_file = args.out / "index.html"
    out_file.write_text(html, encoding="utf-8")
    k = data["views"]["All|All|all"]["kpis"]
    print(f"Built {out_file} from {len(records):,} records ({k['stays']:,} surgeries)")


if __name__ == "__main__":
    main()
