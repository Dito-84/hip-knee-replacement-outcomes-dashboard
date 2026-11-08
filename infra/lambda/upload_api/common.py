"""Shared helpers for the upload API's two functions."""

import json
import re

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def caller(event: dict) -> str | None:
    """The signed-in user's Cognito `sub`, from the claims API Gateway has already verified.

    Never taken from the request path or body, so a user can only ever act on their own
    uploads. None if it's missing or isn't a UUID (Cognito subs always are).
    """
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    sub = str(claims.get("sub", ""))
    return sub if UUID.match(sub) else None


def respond(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }
