"""Cognito pre-authentication and pre-token-generation trigger: refuse demo accounts past their expiry.

Every reviewer account is created by scripts/demo_user.py with custom:valid_until
(ISO 8601, UTC). Fails closed: an account with no expiry, or one that can't be
read, can't sign in either. The retired custom:expires_at is ignored.

Cognito doesn't run pre-authentication when a refresh token is used, so the same
function is also the pre-token-generation trigger, which does run then: an expired
account can't get new tokens either way. (The upload page discards the refresh
token anyway.) Access ends at expiry plus, at most, the remaining life of an access
token (30 minutes). Returning the event unchanged leaves the tokens' claims as they are.

custom:valid_until is immutable, so nobody can change it after the account is
created: not the user with their own token, and not an administrator either.
"""

from datetime import UTC, datetime


class AccountExpired(Exception):
    pass


def handler(event, context):
    raw = str(event.get("request", {}).get("userAttributes", {}).get("custom:valid_until", ""))
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise AccountExpired("This demo account has no valid expiry date. Ask for a new invitation.") from None
    if expires.tzinfo is None:
        raise AccountExpired("This demo account has no valid expiry date. Ask for a new invitation.")
    if datetime.now(UTC) >= expires:
        raise AccountExpired("This demo account has expired. Ask for a new invitation.")
    return event
