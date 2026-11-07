"""Run the dashboard's data-quality checks on a file uploaded to the intake bucket.

Triggered by S3 when an object lands under incoming/. For each file it:

1. loads it with outcomes.load (the same loader the dashboard build uses),
2. runs outcomes.quality.run_checks,
3. compares it with the reference snapshot for drift (drift.py; what alerts is
   decided in drift_policy.py),
4. writes a JSON report to reports/<file name>.json in the same bucket, and
5. publishes a summary to the SNS topic.

Reports and notifications contain only counts, column names and check names,
never values from the records, so they can't leak anything from the file.

The reference snapshot is read-only here: the function can read that one
object and has no permission to write it. It is replaced only deliberately,
with scripts/publish_reference.py.
"""

import json
import os
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

import drift
from outcomes.load import DataFormatError, load, load_raw
from outcomes.quality import run_checks

REPORT_PREFIX = os.environ.get("REPORT_PREFIX", "reports/")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
REFERENCE_BUCKET = os.environ.get("REFERENCE_BUCKET", "")
REFERENCE_KEY = os.environ.get("REFERENCE_KEY", "reference/discharges.csv")
SUPPORTED = {".csv", ".xlsx", ".xlsm", ".xls"}

s3 = boto3.client("s3")
sns = boto3.client("sns")


def check_drift(path: Path, reference: Path) -> dict:
    """Drift of one local file against the reference file, counts-only."""
    try:
        return drift.check(load_raw(reference), load_raw(path))
    except Exception as err:   # never let the drift check stop the data check; no message, it may hold values
        return {"status": "error", "reason": f"drift check failed ({type(err).__name__})"}


def check_file(path: Path, reference: Path | None = None) -> dict:
    """Check one local file and return the report (no AWS calls, so it's easy to test).

    With a reference file, the report also has a "drift" section.
    """
    report: dict = {"file": path.name, "checked_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if path.suffix.lower() not in SUPPORTED:
        return {**report, "status": "skipped", "reason": f"unsupported file type {path.suffix or '(none)'}"}
    try:
        records = load(path)
    except DataFormatError as err:
        return {**report, "status": "unreadable", "reason": str(err)}

    findings = run_checks(records)
    errors = sum(f.severity == "error" for f in findings)
    warnings = sum(f.severity == "warning" for f in findings)
    report = {
        **report,
        "status": "failed" if errors else "passed",
        "records": len(records),
        "errors": errors,
        "warnings": warnings,
        "findings": [asdict(f) for f in findings],
    }
    if reference is not None:
        report["drift"] = check_drift(path, reference)
    return report


def fetch_reference(folder: Path) -> tuple[Path | None, dict]:
    """Download the reference snapshot (read-only) and describe which version was used."""
    if not REFERENCE_BUCKET:
        return None, {"reason": "no reference configured"}
    local = folder / ("reference" + PurePosixPath(REFERENCE_KEY).suffix)
    try:
        obj = s3.get_object(Bucket=REFERENCE_BUCKET, Key=REFERENCE_KEY)
        local.write_bytes(obj["Body"].read())
    except ClientError as err:
        return None, {"reason": f"reference not available ({err.response['Error'].get('Code', 'error')})"}
    return local, {"key": REFERENCE_KEY, "version_id": obj.get("VersionId")}


def summary(report: dict) -> tuple[str, str]:
    """Subject and body for the SNS notification."""
    name, status = report["file"], report["status"]
    subject = f"Data check {status.upper()}: {name}"[:100]   # SNS subject limit
    if status in ("passed", "failed"):
        lines = [f"{report['records']:,} records: {report['errors']} error(s), {report['warnings']} warning(s)."]
        lines += [f"[{f['severity'].upper()}] {f['check']}: {f['message']}" for f in report["findings"]]
    else:
        lines = [report["reason"]]

    found = report.get("drift")
    if found:
        version = (found.get("reference") or {}).get("version_id") or "unknown"
        if found["status"] == "alert":
            subject = f"Data check {status.upper()} + DRIFT: {name}"[:100]
            lines.append(f"\nDrift against the reference (version {version}): "
                         f"{len(found['alerts'])} column(s) need a look.")
            lines += [f"[DRIFT] {a['column']}: {'; '.join(a['reasons'])}" for a in found["alerts"]]
        elif found["status"] == "ok":
            lines.append(f"\nDrift against the reference (version {version}): no alerts; "
                         f"{len(found['logged'])} smaller change(s) in the report.")
        else:
            lines.append(f"\nDrift check skipped: {found['reason']}.")
    return subject, "\n".join(lines)


def handler(event, context):
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        name = PurePosixPath(key).name

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / name
            s3.download_file(bucket, key, str(local))
            reference, about = fetch_reference(Path(tmp))
            report = check_file(local, reference)
        if "drift" in report:
            report["drift"]["reference"] = about
        elif report["status"] in ("passed", "failed"):
            report["drift"] = {"status": "skipped", **about}
        report["source_key"] = key

        report_key = f"{REPORT_PREFIX}{name}.json"
        s3.put_object(Bucket=bucket, Key=report_key, Body=json.dumps(report, indent=2).encode(),
                      ContentType="application/json")
        if TOPIC_ARN:
            subject, body = summary(report)
            sns.publish(TopicArn=TOPIC_ARN, Subject=subject, Message=f"{body}\n\nFull report: s3://{bucket}/{report_key}")

        found = report.get("drift", {})
        print(json.dumps({"file": name, "status": report["status"], "report": report_key,
                          "drift": found.get("status"), "drift_alerts": found.get("alerts", []),
                          "drift_logged": found.get("logged", [])}))
        results.append({"key": key, "status": report["status"]})
    return {"checked": results}
