"""End-to-end check of the deployed upload path, including every refusal.

CI can't reach AWS, so this is the proof that the deny paths work for real. Run it
from infra/ with its virtual environment active, after each cdk deploy that touches
uploads:

    python ../scripts/smoke_upload.py

It creates a throwaway account (no email is sent), runs the checks below with it,
then deletes it, whatever happens. It uses 3 of the day's uploads. Only synthetic
sample batches are uploaded.

  1. the API refuses a request with no token (401)
  2. the user can't change their own expiry date (or the retired expiry attribute)
  3. another (made-up) upload ID gives 404, not someone else's result
  4. S3 refuses a file over 5 MB
  5. a file without the synthetic marker is rejected, and deleted
  6. the clean sample passes, and is deleted
  7. an expired account can't sign in (a second throwaway account, created expired)
"""

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from outcomes.samples import SAMPLES  # noqa: E402

STACK = "HipKneeDashboard"


def outputs(session) -> dict:
    stack = session.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def request(method, url, token=None, body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()
    except (urllib.error.URLError, ConnectionError):
        return 0, b""   # the server closed the connection (S3 may do this to an oversized upload)


def multipart(fields: dict, data: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="batch.csv"\r\n'
                 f"Content-Type: text/csv\r\n\r\n".encode() + data + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def upload(api, token, data: bytes) -> tuple[int, str]:
    status, body = request("POST", f"{api}/uploads", token)
    if status != 201:
        sys.exit(f"POST /uploads returned {status}: {body[:200]!r}")
    grant = json.loads(body)
    payload, content_type = multipart(grant["fields"], data)
    status, _ = request("POST", grant["url"], body=payload, headers={"Content-Type": content_type})
    return status, grant["upload_id"]


def result(api, token, upload_id, wait=120) -> dict:
    until = time.time() + wait
    while time.time() < until:
        time.sleep(3)
        status, body = request("GET", f"{api}/uploads/{upload_id}", token)
        if status == 200:
            return json.loads(body)
        if status != 404:
            sys.exit(f"GET /uploads/{upload_id} returned {status}")
    sys.exit(f"No result for {upload_id} after {wait} s")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=None)
    args = parser.parse_args(argv)
    session = boto3.Session(profile_name=args.profile)
    out = outputs(session)
    pool, client, api = out["UserPoolId"], out["UserPoolClientId"], out["UploadApiUrl"].rstrip("/")
    bucket = out["IntakeBucketName"]
    cognito, s3 = session.client("cognito-idp"), session.client("s3")

    email = f"smoke-{secrets.token_hex(4)}@example.com"
    password = secrets.token_urlsafe(18) + "aA1!"
    soon = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    def create(address, valid_until):
        cognito.admin_create_user(
            UserPoolId=pool, Username=address, MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": address}, {"Name": "email_verified", "Value": "true"},
                            {"Name": "custom:valid_until", "Value": valid_until}])
        cognito.admin_set_user_password(UserPoolId=pool, Username=address, Password=password, Permanent=True)

    def sign_in(address):
        return cognito.admin_initiate_auth(
            UserPoolId=pool, ClientId=client, AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": address, "PASSWORD": password})["AuthenticationResult"]["AccessToken"]

    expired_email = f"smoke-expired-{secrets.token_hex(4)}@example.com"
    created = []
    passed = []

    def ok(step):
        passed.append(step)
        print(f"PASS  {step}")

    try:
        created.append(email)
        create(email, soon)
        token = sign_in(email)
        sub = cognito.get_user(AccessToken=token)["Username"]

        status, _ = request("GET", f"{api}/uploads/{uuid.uuid4()}")
        assert status == 401, f"no token gave {status}"
        ok("1. no token: 401")

        for attribute in ("custom:valid_until", "custom:expires_at"):
            try:
                cognito.update_user_attributes(
                    AccessToken=token, UserAttributes=[{"Name": attribute, "Value": "2099-01-01T00:00:00Z"}])
                raise AssertionError(f"the user could change {attribute}")
            except ClientError as err:
                assert err.response["Error"]["Code"] in ("NotAuthorizedException", "InvalidParameterException"), err
        ok("2. user can't change custom:valid_until (or the retired custom:expires_at)")

        status, _ = request("GET", f"{api}/uploads/{uuid.uuid4()}", token)
        assert status == 404, f"unknown upload ID gave {status}"
        ok("3. someone else's (made-up) upload ID: 404")

        def absent(key):
            try:
                s3.head_object(Bucket=bucket, Key=key)
            except ClientError as err:
                return err.response["Error"]["Code"] in ("404", "NoSuchKey")
            return False

        clean = SAMPLES["clean"][2]().to_csv(index=False).encode()
        too_big = clean + b"\n" * (5 * 1024 * 1024)
        status, upload_id = upload(api, token, too_big)
        assert status in (400, 0), f"a 6 MB file gave {status}"
        assert absent(f"incoming/web/{sub}/{upload_id}.csv"), "the oversized file was stored"
        ok("4. over 5 MB: S3 refused it, nothing stored")

        no_marker = SAMPLES["no-marker"][2]().to_csv(index=False).encode()
        status, upload_id = upload(api, token, no_marker)
        assert status in (201, 204), f"upload gave {status}"
        r = result(api, token, upload_id)
        assert r["status"] == "rejected", r
        assert absent(f"incoming/web/{sub}/{upload_id}.csv"), "the rejected file is still in the bucket"
        ok(f"5. no marker: {r['status']} ({r['reason']}); file deleted")

        status, upload_id = upload(api, token, clean)
        r = result(api, token, upload_id)
        assert r["status"] == "passed" and r["errors"] == 0, r
        assert absent(f"incoming/web/{sub}/{upload_id}.csv"), "the checked file is still in the bucket"
        ok(f"6. clean sample: passed, {r['records']:,} records, drift {r['drift']['status']}; file deleted")

        created.append(expired_email)
        create(expired_email, "2020-01-01T00:00:00Z")
        try:
            sign_in(expired_email)
            raise AssertionError("an expired account signed in")
        except ClientError as err:
            assert err.response["Error"]["Code"] == "UserLambdaValidationException", err
        ok("7. expired account: sign-in refused")
    finally:
        for address in created:
            cognito.admin_delete_user(UserPoolId=pool, Username=address)
            print(f"Deleted the throwaway account {address}")
    print(f"\nAll {len(passed)} checks passed.")


if __name__ == "__main__":
    main()
