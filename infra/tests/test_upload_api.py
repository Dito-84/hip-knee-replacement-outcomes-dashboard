"""The upload API's functions and the sign-in expiry trigger, on made-up events.

DynamoDB and S3 are in-memory fakes, so no AWS account is needed.
"""

import importlib
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
UPLOAD_API = ROOT / "infra" / "lambda" / "upload_api"
PRE_AUTH = ROOT / "infra" / "lambda" / "pre_auth"
SUB = "11111111-2222-4333-8444-555555555555"
OTHER = "99999999-8888-4777-8666-555555555555"
UPLOAD_ID = "0b7e3f1c-0000-4000-8000-000000000001"


def load(folder: Path, name: str):
    sys.path.insert(0, str(folder))
    try:
        sys.modules.pop(name, None)
        sys.modules.pop("common", None)
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(folder))


def event(sub=SUB, **extra):
    claims = {"sub": sub} if sub is not None else {}
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}, **extra}


class FakeDynamo:
    """Applies the transaction's conditions like DynamoDB: all items or none."""

    def __init__(self):
        self.items = {}

    def transact_write_items(self, TransactItems):
        updates = [t["Update"] for t in TransactItems]
        for u in updates:
            limit = int(u["ExpressionAttributeValues"][":limit"]["N"])
            if self.items.get(u["Key"]["pk"]["S"], 0) >= limit:
                raise ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "x"}},
                                  "TransactWriteItems")
        for u in updates:
            pk = u["Key"]["pk"]["S"]
            self.items[pk] = self.items.get(pk, 0) + 1


@pytest.fixture
def presign(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("BUCKET", "intake")
    monkeypatch.setenv("TABLE", "quota")
    monkeypatch.setenv("PER_USER_DAILY", "3")
    monkeypatch.setenv("ALL_USERS_DAILY", "5")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    module = load(UPLOAD_API, "presign")
    module.dynamodb = FakeDynamo()
    return module


def body(response):
    return json.loads(response["body"])


def test_presigned_post_fixes_key_size_type_and_expiry(presign):
    response = presign.handler(event(), None)
    assert response["statusCode"] == 201
    grant = body(response)
    key = grant["fields"]["key"]
    assert key == f"incoming/web/{SUB}/{grant['upload_id']}.csv"
    assert grant["url"] == "https://intake.s3.amazonaws.com/"   # the host the page's CSP allows
    policy = json.loads(__import__("base64").b64decode(grant["fields"]["policy"]))
    conditions = policy["conditions"]
    assert ["content-length-range", 1, 5 * 1024 * 1024] in conditions
    assert {"key": key} in conditions and {"bucket": "intake"} in conditions
    assert {"Content-Type": "text/csv"} in conditions
    expires = datetime.fromisoformat(policy["expiration"].replace("Z", "+00:00"))
    assert timedelta(minutes=4) < expires - datetime.now(UTC) <= timedelta(minutes=5)
    assert response["headers"]["Cache-Control"] == "no-store"


def test_every_upload_gets_its_own_key(presign):
    keys = {body(presign.handler(event(), None))["fields"]["key"] for _ in range(3)}
    assert len(keys) == 3


@pytest.mark.parametrize("sub", [None, "", "not-a-uuid", "../../reports/x", "AAAAAAAA-2222-4333-8444-555555555555"])
def test_no_valid_caller_means_401_and_no_upload_counted(presign, sub):
    assert presign.handler(event(sub), None)["statusCode"] == 401
    assert presign.dynamodb.items == {}


def test_per_user_daily_limit(presign):
    statuses = [presign.handler(event(), None)["statusCode"] for _ in range(4)]
    assert statuses == [201, 201, 201, 429]
    # A refused upload isn't counted against anyone
    assert sorted(presign.dynamodb.items.values()) == [3, 3]


def test_all_users_daily_limit_caps_the_total(presign):
    statuses = [presign.handler(event(SUB), None)["statusCode"] for _ in range(3)]
    statuses += [presign.handler(event(OTHER), None)["statusCode"] for _ in range(3)]
    assert statuses == [201, 201, 201, 201, 201, 429]   # 5 in all, though OTHER had 1 left


def test_counters_expire_after_two_days(presign):
    captured = {}
    presign.dynamodb.transact_write_items = lambda TransactItems: captured.update(items=TransactItems)
    now = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)
    assert presign.take_upload(SUB, now)
    pks = [t["Update"]["Key"]["pk"]["S"] for t in captured["items"]]
    assert pks == [f"user#{SUB}#2026-11-08", "all#2026-11-08"]
    ttl = int(captured["items"][0]["Update"]["ExpressionAttributeValues"][":expires"]["N"])
    assert ttl == int((now + timedelta(days=2)).timestamp())


# ---------- GET /uploads/{id} ----------

class FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.asked = []

    def get_object(self, Bucket, Key):
        self.asked.append(Key)
        if Key not in self.objects:
            # Without s3:ListBucket, S3 says AccessDenied for a missing key
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}


REPORT = {
    "file": "x.csv", "status": "failed", "checked_at": "2026-11-08T15:00:00+00:00", "records": 120,
    "errors": 1, "warnings": 0, "source_key": f"incoming/web/{SUB}/{UPLOAD_ID}.csv",
    "findings": [{"severity": "error", "check": "discharge before admission", "message": "5 stay(s)",
                  "extra": "not passed on"}],
    "drift": {"status": "alert", "reference": {"version_id": "v1"},
              "alerts": [{"column": "measure", "reasons": ["1 new value(s)"], "psi": 0.4}],
              "logged": [{"column": "a"}, {"column": "b"}], "columns": [{"column": "measure"}]},
}


@pytest.fixture
def results(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("BUCKET", "intake")
    module = load(UPLOAD_API, "results")
    module.s3 = FakeS3({f"reports/web/{SUB}/{UPLOAD_ID}.csv.json": REPORT})
    return module


def test_result_is_an_explicit_counts_only_subset(results):
    response = results.handler(event(pathParameters={"id": UPLOAD_ID}), None)
    assert response["statusCode"] == 200
    out = body(response)
    assert out == {
        "status": "failed", "checked_at": "2026-11-08T15:00:00+00:00", "records": 120, "errors": 1, "warnings": 0,
        "findings": [{"severity": "error", "check": "discharge before admission", "message": "5 stay(s)"}],
        "drift": {"status": "alert", "alerts": [{"column": "measure", "reasons": ["1 new value(s)"]}], "logged": 2},
    }
    assert "source_key" not in out and "columns" not in json.dumps(out)


def test_results_are_only_ever_read_from_the_callers_own_folder(results):
    response = results.handler(event(OTHER, pathParameters={"id": UPLOAD_ID}), None)
    assert response["statusCode"] == 404 and body(response) == {"status": "pending"}
    assert results.s3.asked == [f"reports/web/{OTHER}/{UPLOAD_ID}.csv.json"]


@pytest.mark.parametrize("upload_id", ["", "../x", f"{UPLOAD_ID}/../../y", "x" * 36, UPLOAD_ID.upper()])
def test_malformed_ids_are_refused_without_reading_s3(results, upload_id):
    assert results.handler(event(pathParameters={"id": upload_id}), None)["statusCode"] == 400
    assert results.s3.asked == []


def test_results_need_a_valid_caller(results):
    assert results.handler(event(None, pathParameters={"id": UPLOAD_ID}), None)["statusCode"] == 401


def test_other_s3_errors_are_not_hidden(results):
    def broken(**_):
        raise ClientError({"Error": {"Code": "SlowDown", "Message": "x"}}, "GetObject")
    results.s3.get_object = broken
    with pytest.raises(ClientError):
        results.handler(event(pathParameters={"id": UPLOAD_ID}), None)


# ---------- Pre-authentication trigger ----------

@pytest.fixture
def pre_auth():
    return load(PRE_AUTH, "handler")


def auth_event(valid_until=None, **extra):
    attrs = {"email": "reviewer@example.com", **extra}
    if valid_until is not None:
        attrs["custom:valid_until"] = valid_until
    return {"request": {"userAttributes": attrs}, "response": {}}


def test_sign_in_allowed_before_expiry(pre_auth):
    soon = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    e = auth_event(soon)
    assert pre_auth.handler(e, None) is e


def test_the_retired_expiry_attribute_is_ignored(pre_auth):
    future = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(pre_auth.AccountExpired):
        pre_auth.handler(auth_event(None, **{"custom:expires_at": future}), None)


@pytest.mark.parametrize("valid_until", [
    "2020-01-01T00:00:00Z",           # expired
    None,                             # no expiry: fails closed
    "",
    "next week",                      # unreadable
    "2099-01-01T00:00:00",            # no time zone: refused rather than guessed
])
def test_sign_in_refused_after_expiry_or_without_a_valid_one(pre_auth, valid_until):
    with pytest.raises(pre_auth.AccountExpired):
        pre_auth.handler(auth_event(valid_until), None)


# ---------- scripts/demo_user.py ----------

def test_demo_accounts_expire_at_the_end_of_their_last_day():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        demo_user = importlib.import_module("demo_user")
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    now = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)
    assert demo_user.expiry(1, now) == "2026-11-08T23:59:59Z"
    assert demo_user.expiry(7, now) == "2026-11-14T23:59:59Z"
    assert 20 <= len(demo_user.expiry(7, now)) <= 25   # the custom attribute's length limits
