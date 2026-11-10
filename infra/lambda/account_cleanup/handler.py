"""Daily clean-up: delete demo accounts a week after they expired.

An expired account can't sign in (the expiry trigger refuses it), but it still holds the
reviewer's email address, which is personal data that nothing needs once the demo is over.
Deleting it used to depend on remembering to run `demo_user.py delete`. This function does it
on a schedule, GRACE_DAYS after the account's custom:valid_until, so a reviewer who asks for
more time within the week can still be recreated with the same address.

Accounts with no valid_until (none should exist; the trigger refuses them anyway) are left
alone and counted, so a person decides. Logs and the return value are counts only: never an
email address.
"""

import os
from datetime import UTC, datetime, timedelta

import boto3

ATTRIBUTE = "custom:valid_until"
FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def due(users, now: datetime, grace: timedelta) -> tuple[list[str], int]:
    """(usernames to delete, how many accounts have no readable expiry)."""
    delete, unknown = [], 0
    for user in users:
        attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
        try:
            valid_until = datetime.strptime(attrs[ATTRIBUTE], FORMAT).replace(tzinfo=UTC)
        except (KeyError, ValueError):
            unknown += 1
            continue
        if valid_until + grace < now:
            delete.append(user["Username"])
    return delete, unknown


def handler(event, context, cognito=None, now=None):
    cognito = cognito or boto3.client("cognito-idp")
    pool = os.environ["USER_POOL_ID"]
    grace = timedelta(days=int(os.environ.get("GRACE_DAYS", "7")))
    users = [u for page in cognito.get_paginator("list_users").paginate(
        UserPoolId=pool, AttributesToGet=[ATTRIBUTE]) for u in page["Users"]]
    delete, unknown = due(users, now or datetime.now(UTC), grace)

    failed = 0
    for username in delete:
        try:
            cognito.admin_delete_user(UserPoolId=pool, Username=username)
        except cognito.exceptions.UserNotFoundException:
            pass                                    # already gone: the goal is met
        except Exception:                           # noqa: BLE001 - counted, then raised below
            failed += 1
    summary = {"accounts": len(users), "deleted": len(delete) - failed, "failed": failed,
               "no_expiry": unknown}
    print(f"Account clean-up: {summary}")
    if failed:
        # The error alarm emails; the type and count are enough, never an address
        raise RuntimeError(f"{failed} expired account(s) couldn't be deleted")
    return summary
