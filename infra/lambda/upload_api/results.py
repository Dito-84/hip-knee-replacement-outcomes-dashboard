"""GET /uploads/{id}: the counts-only result of one of the caller's own uploads.

Reads reports/web/<caller's sub>/<id>.csv.json, written by the data-check Lambda.
The sub comes from the verified token, never from the request, so one user can't
read another's results. 404 means "not checked yet": the page keeps polling.

The report is already counts-only; this returns an explicit subset of it anyway,
so a field added to reports later doesn't reach the browser without a decision.
"""

import json
import os

import boto3
from botocore.exceptions import ClientError
from common import UUID, caller, respond

BUCKET = os.environ.get("BUCKET", "")
PREFIX = "reports/web/"
KEEP = ("status", "checked_at", "records", "errors", "warnings", "reason")

s3 = boto3.client("s3")


def summarize(report: dict) -> dict:
    out = {k: report[k] for k in KEEP if k in report}
    if "findings" in report:
        out["findings"] = [{"severity": f["severity"], "check": f["check"], "message": f["message"]}
                           for f in report["findings"]]
    drift = report.get("drift")
    if drift:
        out["drift"] = {
            "status": drift.get("status"),
            "alerts": [{"column": a["column"], "reasons": a["reasons"]} for a in drift.get("alerts", [])],
            "logged": len(drift.get("logged", [])),
        }
    return out


def handler(event, context):
    sub = caller(event)
    if not sub:
        return respond(401, {"message": "Not signed in."})
    upload_id = str((event.get("pathParameters") or {}).get("id", ""))
    if not UUID.match(upload_id):
        return respond(400, {"message": "Not an upload ID."})
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=f"{PREFIX}{sub}/{upload_id}.csv.json")
    except ClientError as err:
        # Without s3:ListBucket (deliberately not granted), S3 answers AccessDenied for a key
        # that doesn't exist yet, so both mean "no result yet"
        if err.response["Error"].get("Code") in ("NoSuchKey", "AccessDenied", "404"):
            return respond(404, {"status": "pending"})
        raise
    return respond(200, summarize(json.loads(obj["Body"].read())))
