"""The daily clean-up of expired demo accounts (infra/lambda/account_cleanup)."""

import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

FOLDER = Path(__file__).resolve().parents[1] / "lambda" / "account_cleanup"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def cleanup(monkeypatch):
    monkeypatch.setenv("USER_POOL_ID", "us-east-1_EXAMPLE1")
    monkeypatch.setenv("GRACE_DAYS", "7")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    sys.path.insert(0, str(FOLDER))
    try:
        sys.modules.pop("handler", None)
        yield importlib.import_module("handler")
    finally:
        sys.path.remove(str(FOLDER))
        sys.modules.pop("handler", None)


class NotFound(Exception):
    pass


class FakeCognito:
    """list_users (two pages) and admin_delete_user; usernames, never emails, are what's deleted."""

    class exceptions:
        UserNotFoundException = NotFound

    def __init__(self, users, fail=(), gone=()):
        self.users, self.fail, self.gone, self.deleted = users, set(fail), set(gone), []

    def get_paginator(self, name):
        assert name == "list_users"
        users = self.users

        class Paginator:
            def paginate(self, **kwargs):
                assert kwargs == {"UserPoolId": "us-east-1_EXAMPLE1", "AttributesToGet": ["custom:valid_until"]}
                half = len(users) // 2
                return [{"Users": users[:half]}, {"Users": users[half:]}]
        return Paginator()

    def admin_delete_user(self, UserPoolId, Username):
        if Username in self.fail:
            raise RuntimeError("throttled")
        if Username in self.gone:
            raise NotFound()
        self.deleted.append(Username)


def user(name, valid_until=None):
    attrs = [] if valid_until is None else [{"Name": "custom:valid_until", "Value": valid_until}]
    return {"Username": name, "Attributes": attrs}


USERS = [
    user("expired-8-days", "2026-11-12T11:59:59Z"),   # more than 7 days ago: deleted
    user("expired-6-days", "2026-11-14T23:59:59Z"),   # within the week: kept, so it can still be recreated
    user("still-valid", "2026-11-30T23:59:59Z"),
    user("no-expiry"),                                # shouldn't exist; counted, left for a person
    user("bad-expiry", "next week"),
]


def test_only_accounts_a_week_past_expiry_are_deleted(cleanup):
    cognito = FakeCognito(USERS)
    summary = cleanup.handler({}, None, cognito=cognito, now=NOW)
    assert cognito.deleted == ["expired-8-days"]
    assert summary == {"accounts": 5, "deleted": 1, "failed": 0, "no_expiry": 2}


def test_exactly_at_the_end_of_the_grace_period_is_kept(cleanup):
    cognito = FakeCognito([user("edge", "2026-11-13T12:00:00Z")])   # expiry + 7 days == now
    assert cleanup.handler({}, None, cognito=cognito, now=NOW)["deleted"] == 0


def test_an_account_already_gone_is_not_a_failure(cleanup):
    cognito = FakeCognito(USERS, gone={"expired-8-days"})
    assert cleanup.handler({}, None, cognito=cognito, now=NOW)["failed"] == 0


def test_a_failed_delete_raises_so_the_alarm_emails_but_never_names_the_account(cleanup, capsys):
    cognito = FakeCognito(USERS + [user("old@example.com", "2026-01-01T00:00:00Z")], fail={"old@example.com"})
    with pytest.raises(RuntimeError, match=r"^1 expired account\(s\) couldn't be deleted$"):
        cleanup.handler({}, None, cognito=cognito, now=NOW)
    assert cognito.deleted == ["expired-8-days"]           # the others still went
    assert "example.com" not in capsys.readouterr().out    # logs are counts only
