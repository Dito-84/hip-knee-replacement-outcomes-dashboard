"""POST /uploads: give the signed-in user a presigned S3 POST for one synthetic CSV.

The POST policy, which S3 itself enforces, fixes:
- the exact key: incoming/web/<caller's sub>/<new UUID>.csv,
- the size: 1 byte to MAX_BYTES,
- Content-Type: text/csv (a label the browser sends, not a check of the contents:
  the data-check Lambda's synthetic-only guard is the real check),
- the expiry: EXPIRES_IN seconds.

Before signing, it takes one upload from two daily limits in DynamoDB, in one
transaction: the caller's own (PER_USER_DAILY) and everyone's together
(ALL_USERS_DAILY, which caps what the data check can cost). HTTP APIs have no
per-user usage plans, so this is where per-user limits live.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from common import caller, respond

BUCKET = os.environ.get("BUCKET", "")
TABLE = os.environ.get("TABLE", "")
MAX_BYTES = int(os.environ.get("MAX_BYTES", str(5 * 1024 * 1024)))
EXPIRES_IN = int(os.environ.get("EXPIRES_IN", "300"))
PER_USER_DAILY = int(os.environ.get("PER_USER_DAILY", "20"))
ALL_USERS_DAILY = int(os.environ.get("ALL_USERS_DAILY", "200"))
PREFIX = "incoming/web/"

# Virtual-hosted URLs (https://<bucket>.s3.amazonaws.com/), which the page's CSP allows by name
s3 = boto3.client("s3", config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
dynamodb = boto3.client("dynamodb")


def _counter(pk: str, limit: int, expires: int) -> dict:
    return {"Update": {
        "TableName": TABLE,
        "Key": {"pk": {"S": pk}},
        "UpdateExpression": "ADD uploads :one SET expires = :expires",
        "ConditionExpression": "attribute_not_exists(uploads) OR uploads < :limit",
        "ExpressionAttributeValues": {":one": {"N": "1"}, ":expires": {"N": str(expires)},
                                      ":limit": {"N": str(limit)}},
    }}


def take_upload(sub: str, now: datetime) -> bool:
    """Count one upload against both daily limits; False if either is used up."""
    day = now.strftime("%Y-%m-%d")
    expires = int((now + timedelta(days=2)).timestamp())   # DynamoDB TTL removes old counters
    try:
        dynamodb.transact_write_items(TransactItems=[
            _counter(f"user#{sub}#{day}", PER_USER_DAILY, expires),
            _counter(f"all#{day}", ALL_USERS_DAILY, expires),
        ])
    except ClientError as err:
        if err.response["Error"].get("Code") == "TransactionCanceledException":
            return False   # a limit was reached (or two uploads collided; trying again works)
        raise
    return True


def handler(event, context):
    sub = caller(event)
    if not sub:
        return respond(401, {"message": "Not signed in."})
    if not take_upload(sub, datetime.now(UTC)):
        return respond(429, {"message": "The daily upload limit has been reached. Try again tomorrow."})

    upload_id = str(uuid.uuid4())
    post = s3.generate_presigned_post(
        Bucket=BUCKET,
        Key=f"{PREFIX}{sub}/{upload_id}.csv",
        Fields={"Content-Type": "text/csv"},
        Conditions=[{"Content-Type": "text/csv"}, ["content-length-range", 1, MAX_BYTES]],
        ExpiresIn=EXPIRES_IN,
    )
    return respond(201, {"upload_id": upload_id, "url": post["url"], "fields": post["fields"],
                         "max_bytes": MAX_BYTES, "expires_in": EXPIRES_IN})
