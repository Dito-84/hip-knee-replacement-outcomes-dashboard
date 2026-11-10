"""Create, list, disable and delete time-limited demo accounts for the upload page.

Run from infra/ with its virtual environment active, using your own AWS profile:

    python ../scripts/demo_user.py create reviewer@example.com --days 7
    python ../scripts/demo_user.py list
    python ../scripts/demo_user.py disable reviewer@example.com
    python ../scripts/demo_user.py delete reviewer@example.com

create: Cognito emails the reviewer a temporary password and the sign-in link;
they choose their own password on first sign-in. Nobody shares a password.
The account stops being able to sign in at the end of its last day (UTC), enforced
by the pre-authentication trigger; an access token already issued lasts at most
30 minutes more. The date can't be changed afterwards (it's immutable, so a user
can't extend it): to give someone more time, delete the account and create it again.

disable: stops sign-in at once and revokes the account's refresh tokens.
delete: removes the account and its email address from Cognito. Do this after a demo.
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta

import boto3

STACK = "HipKneeDashboard"


def stack_output(cloudformation, name: str) -> str:
    outputs = cloudformation.describe_stacks(StackName=STACK)["Stacks"][0].get("Outputs", [])
    for o in outputs:
        if o["OutputKey"] == name:
            return o["OutputValue"]
    sys.exit(f"The {STACK} stack has no {name} output yet. Run cdk deploy first.")


def expiry(days: int, now: datetime | None = None) -> str:
    """The end of the last day, UTC, as Cognito's custom:valid_until (e.g. 2026-11-15T23:59:59Z)."""
    now = now or datetime.now(UTC)
    last = (now + timedelta(days=days - 1)).date()
    return f"{last.isoformat()}T23:59:59Z"


CLEANUP_GRACE_DAYS = 7   # keep in step with CLEANUP_GRACE_DAYS in infra/dashboard_infra/uploads.py


def lifecycle(valid_until: str | None, now: datetime | None = None) -> str:
    """Where the account is: valid, expired (and when the daily clean-up deletes it), or no expiry."""
    if not valid_until:
        return "no expiry date (can't sign in; the clean-up leaves it for you to delete)"
    try:
        end = datetime.strptime(valid_until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return f"unreadable expiry {valid_until!r} (can't sign in; delete it)"
    now = now or datetime.now(UTC)
    if end >= now:
        return f"valid until {valid_until}"
    deleted = (end + timedelta(days=CLEANUP_GRACE_DAYS)).strftime("%Y-%m-%d")
    return f"EXPIRED {valid_until}; the daily clean-up deletes it after {deleted}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["create", "list", "disable", "delete"])
    parser.add_argument("email", nargs="?")
    parser.add_argument("--days", type=int, default=7, help="days the account works, today included (1 to 30)")
    parser.add_argument("--profile", default=None, help="AWS CLI profile (default: your default profile)")
    args = parser.parse_args(argv)
    if args.action != "list" and not args.email:
        parser.error(f"{args.action} needs an email address")
    if not 1 <= args.days <= 30:
        parser.error("--days must be between 1 and 30")

    session = boto3.Session(profile_name=args.profile)
    pool = stack_output(session.client("cloudformation"), "UserPoolId")
    cognito = session.client("cognito-idp")

    if args.action == "create":
        expires = expiry(args.days)
        cognito.admin_create_user(
            UserPoolId=pool, Username=args.email, DesiredDeliveryMediums=["EMAIL"],
            UserAttributes=[{"Name": "email", "Value": args.email}, {"Name": "email_verified", "Value": "true"},
                            {"Name": "custom:valid_until", "Value": expires}])
        print(f"Created {args.email}; it can sign in until {expires}. Cognito has emailed the invitation "
              "(it can land in spam).")
    elif args.action == "disable":
        cognito.admin_disable_user(UserPoolId=pool, Username=args.email)
        cognito.admin_user_global_sign_out(UserPoolId=pool, Username=args.email)
        print(f"Disabled {args.email} and revoked its sessions (an access token already issued lasts up to "
              "30 minutes more).")
    elif args.action == "delete":
        cognito.admin_delete_user(UserPoolId=pool, Username=args.email)
        print(f"Deleted {args.email}.")
    else:
        for page in cognito.get_paginator("list_users").paginate(UserPoolId=pool):
            for user in page["Users"]:
                attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
                state = "enabled" if user["Enabled"] else "disabled"
                print(f"{attrs.get('email', user['Username'])}\t{state}\t{user['UserStatus']}\t"
                      f"{lifecycle(attrs.get('custom:valid_until'))}")


if __name__ == "__main__":
    main()
