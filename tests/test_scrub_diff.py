"""The stack diff posted on public pull requests never shows the account ID or an email address."""

import sys

from helpers import ROOT

sys.path.insert(0, str(ROOT / "scripts"))
import scrub_diff  # noqa: E402

DIFF = """\x1b[1mStack HipKneeDashboard (aws://123456789012/us-east-1)\x1b[22m
[~] AWS::SNS::Subscription Endpoint: me.name+demo@example.com
arn:aws:iam::123456789012:role/x and arn:aws:iam::999999999999:role/y
Number of stacks with differences: 1
"""


def test_account_ids_emails_and_colour_codes_are_removed():
    out = scrub_diff.scrub(DIFF, "123456789012", "me.name+demo@example.com")
    assert "123456789012" not in out and "999999999999" not in out
    assert "example.com" not in out and "<alert email>" in out
    assert "\x1b" not in out
    assert "aws://<account>/us-east-1" in out


def test_any_email_is_removed_even_if_not_the_alert_email():
    assert scrub_diff.scrub("contact other@corp.org now") == "contact <email> now"


def test_comment_is_marked_headlined_and_capped():
    body = scrub_diff.comment("x" * 70_000 + "\nNumber of stacks with differences: 1", "abcdef1234567")
    assert body.startswith(scrub_diff.MARKER) and "abcdef1" in body
    assert "This pull request changes the AWS stacks." in body
    assert len(body) < 65_536 and "truncated" in body
    assert "No changes" in scrub_diff.comment("There were no differences", "abcdef1")


def test_scrub_in_place(tmp_path):
    path = tmp_path / "diff.txt"
    path.write_text(DIFF)
    scrub_diff.main([str(path), "123456789012", "me.name+demo@example.com"])
    assert "123456789012" not in path.read_text()
