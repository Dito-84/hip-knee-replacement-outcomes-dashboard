"""Build the dashboard as a single self-contained HTML page.

Synthetic data (the public version):
    python build.py                       # -> _site/index.html

Your own data, kept on your computer:
    python build.py --data private/discharges.xlsx --out private/site

A build from any file other than the synthetic dataset must be written
inside private/ (which git ignores) or outside this repository, so real
results can't be committed by accident.

The AWS copy of the site also gets the upload page (upload.html) and its sample
batches (samples/), built only when all four UPLOAD_* environment variables are
set (the "Deploy to AWS" workflow sets them from repository variables) and only
for the synthetic dataset. The GitHub Pages copy has no upload page:
    UPLOAD_API_URL=... UPLOAD_AUTH_DOMAIN=... UPLOAD_CLIENT_ID=... UPLOAD_BUCKET=... python build.py

To share a build, hide small counts (CMS doesn't allow publishing counts of 1 to 10):
    python build.py --data private/discharges.xlsx --out private/site --suppress-under 11

Every build runs the data-quality checks in outcomes/quality.py first and
stops if one finds an error that would make the measures wrong. Use
--skip-checks only after reviewing the findings with scripts/check_data.py.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from outcomes.load import DataFormatError, load
from outcomes.metrics import build_dashboard_data
from outcomes.quality import run_checks
from outcomes.samples import write_samples
from outcomes.suppression import suppress

ROOT = Path(__file__).resolve().parent
SYNTHETIC = ROOT / "data" / "synthetic_discharges.csv"
TEMPLATE = ROOT / "dashboard" / "template.html"
UPLOAD_TEMPLATE = ROOT / "dashboard" / "upload.html"
PLACEHOLDER = "/*__DATA__*/null"
CONFIG_PLACEHOLDER = "/*__CONFIG__*/null"
CSP_PLACEHOLDER = "<!--__CSP__-->"

# Each upload setting must look like what the stack outputs: they go into the page's CSP and script
UPLOAD_SETTINGS = {
    "UPLOAD_API_URL": ("apiUrl", r"https://[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com"),
    "UPLOAD_AUTH_DOMAIN": ("authDomain", r"https://[a-z0-9-]+\.auth\.[a-z0-9-]+\.amazoncognito\.com"),
    "UPLOAD_CLIENT_ID": ("clientId", r"[a-z0-9]{20,40}"),
    "UPLOAD_BUCKET": ("bucket", r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"),
}


def content_security_policy(html):
    """A Content-Security-Policy <meta> tag that allows only what the page needs.

    The page's own script embeds the data, so it changes with every build: its
    SHA-256 hash is computed here and is the only inline script allowed. Plotly
    comes from jsDelivr, pinned by a subresource-integrity hash in the template,
    and the policy names that exact file, not the whole CDN (which serves any npm
    package). Fonts are the system's own. Styles stay inline because Plotly injects them.
    """
    inline = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    external = re.findall(r'<script src="(https://cdn\.jsdelivr\.net/[^"]+)"', html)
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(code.encode("utf-8")).digest()).decode() + "'" for code in inline
    )
    policy = "; ".join([
        "default-src 'none'",
        f"script-src {hashes} {' '.join(external)}",
        "style-src 'unsafe-inline'",
        "img-src data:",
        "base-uri 'none'",
        "form-action 'none'",
    ])
    return f'<meta http-equiv="Content-Security-Policy" content="{policy}">'


def _sha256_sources(html, tag):
    return " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(code.encode("utf-8")).digest()).decode() + "'"
        for code in re.findall(rf"<{tag}>(.*?)</{tag}>", html, flags=re.S)
    )


def upload_config(env=None):
    """The upload page's settings from UPLOAD_* environment variables; None if none are set."""
    env = os.environ if env is None else env
    present = {name: env.get(name, "").strip() for name in UPLOAD_SETTINGS}
    if not any(present.values()):
        return None
    config = {}
    for name, (key, pattern) in UPLOAD_SETTINGS.items():
        if not re.fullmatch(pattern, present[name]):
            sys.exit(f"{name} is missing or doesn't look like the stack's output; set all four UPLOAD_* "
                     f"variables from the cdk deploy outputs, or none.")
        config[key] = present[name]
    config["region"] = config["apiUrl"].split(".")[2]
    return config


def upload_csp(html, config):
    """CSP for upload.html: its own script and style by hash, and connections only to the API,
    the Cognito sign-in domain and the intake bucket's S3 endpoints."""
    bucket, region = config["bucket"], config["region"]
    policy = "; ".join([
        "default-src 'none'",
        f"script-src {_sha256_sources(html, 'script')}",
        f"style-src {_sha256_sources(html, 'style')}",
        f"connect-src {config['apiUrl']} {config['authDomain']} https://{bucket}.s3.amazonaws.com "
        f"https://{bucket}.s3.{region}.amazonaws.com",
        "img-src data:",
        "base-uri 'none'",
        "form-action 'none'",
    ])
    return f'<meta http-equiv="Content-Security-Policy" content="{policy}">'


def build_upload_page(out_dir, config):
    """Write upload.html and the sample batches it offers into out_dir."""
    samples = write_samples(out_dir / "samples")
    page = {k: config[k] for k in ("apiUrl", "authDomain", "clientId")} | {"samples": samples}
    html = UPLOAD_TEMPLATE.read_text(encoding="utf-8")
    if CONFIG_PLACEHOLDER not in html or CSP_PLACEHOLDER not in html:
        sys.exit("Upload template is missing a placeholder")
    html = html.replace(CONFIG_PLACEHOLDER, json.dumps(page).replace("</", "<\\/"))
    html = html.replace(CSP_PLACEHOLDER, upload_csp(html, config))
    (out_dir / "upload.html").write_text(html, encoding="utf-8")
    return out_dir / "upload.html"


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
    html = html.replace(CSP_PLACEHOLDER, content_security_policy(html))

    args.out.mkdir(parents=True, exist_ok=True)
    out_file = args.out / "index.html"
    out_file.write_text(html, encoding="utf-8")
    k = data["views"]["All|All|all"]["kpis"]
    print(f"Built {out_file} from {len(records):,} records ({k['stays']:,} surgeries)")

    config = upload_config()
    if config and synthetic:   # never alongside a build of anything but the synthetic data
        print(f"Built {build_upload_page(args.out, config)} and its sample batches")


if __name__ == "__main__":
    main()
