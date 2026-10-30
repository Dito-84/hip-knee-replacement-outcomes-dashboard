"""Run the dashboard's data-quality checks on a file uploaded to the intake bucket.

Triggered by S3 when an object lands under incoming/. For each file it:

1. loads it with outcomes.load (the same loader the dashboard build uses),
2. runs outcomes.quality.run_checks,
3. writes a JSON report to reports/<file name>.json in the same bucket, and
4. publishes a one-line summary to the SNS topic.

Reports and notifications contain only counts and check names, never values
from the records, so they can't leak anything from the file.
"""

import json
import os
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote_plus

import boto3

from outcomes.load import DataFormatError, load
from outcomes.quality import run_checks

REPORT_PREFIX = os.environ.get("REPORT_PREFIX", "reports/")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
SUPPORTED = {".csv", ".xlsx", ".xlsm", ".xls"}

s3 = boto3.client("s3")
sns = boto3.client("sns")


def check_file(path: Path) -> dict:
    """Check one local file and return the report (no AWS calls, so it's easy to test)."""
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
    return {
        **report,
        "status": "failed" if errors else "passed",
        "records": len(records),
        "errors": errors,
        "warnings": warnings,
        "findings": [asdict(f) for f in findings],
    }


def summary(report: dict) -> tuple[str, str]:
    """Subject and body for the SNS notification."""
    name, status = report["file"], report["status"]
    subject = f"Data check {status.upper()}: {name}"[:100]   # SNS subject limit
    if status in ("passed", "failed"):
        lines = [f"{report['records']:,} records: {report['errors']} error(s), {report['warnings']} warning(s)."]
        lines += [f"[{f['severity'].upper()}] {f['check']}: {f['message']}" for f in report["findings"]]
    else:
        lines = [report["reason"]]
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
            report = check_file(local)
        report["source_key"] = key

        report_key = f"{REPORT_PREFIX}{name}.json"
        s3.put_object(Bucket=bucket, Key=report_key, Body=json.dumps(report, indent=2).encode(),
                      ContentType="application/json")
        if TOPIC_ARN:
            subject, body = summary(report)
            sns.publish(TopicArn=TOPIC_ARN, Subject=subject, Message=f"{body}\n\nFull report: s3://{bucket}/{report_key}")

        print(json.dumps({"file": name, "status": report["status"], "report": report_key}))
        results.append({"key": key, "status": report["status"]})
    return {"checked": results}
