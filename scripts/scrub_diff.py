"""Make `cdk diff` output safe to post on a public repository, and format it as a comment.

    python scripts/scrub_diff.py diff.txt ACCOUNT_ID ALERT_EMAIL     # scrub the file in place
    python scripts/scrub_diff.py --comment diff.txt COMMIT_SHA       # print the PR comment

Scrubbing replaces the AWS account ID and the alert email (and anything shaped like
an email address) with placeholders, and drops terminal colour codes. Comments stay
under GitHub's 65,536-character limit.
"""

import re
import sys
from pathlib import Path

MARKER = "<!-- stack-diff -->"
LIMIT = 60_000
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def scrub(text: str, account: str = "", email: str = "") -> str:
    text = ANSI.sub("", text)
    if email:
        text = text.replace(email, "<alert email>")
    text = EMAIL.sub("<email>", text)
    if account:
        text = text.replace(account, "<account>")
    return re.sub(r"\b\d{12}\b", "<account>", text)   # any other 12-digit account ID


def comment(diff: str, sha: str) -> str:
    changed = "There were no differences" not in diff and "Number of stacks with differences: 0" not in diff
    if len(diff) > LIMIT:
        diff = diff[:LIMIT] + "\n… (truncated; the full diff is in the job's log)"
    headline = "This pull request changes the AWS stacks." if changed else "No changes to the AWS stacks."
    return (f"{MARKER}\n### `cdk diff` for {sha[:7]}\n\n{headline} Merging deploys the website straight away; "
            f"a change to `HipKneeDashboard` then waits for approval in the `production` environment, and "
            f"`HipKneeDashboardCi` is deployed by hand. Template comparison: the deploy's replacement guard "
            f"checks the change set itself.\n\n"
            f"<details><summary>Diff</summary>\n\n```\n{diff.strip()}\n```\n</details>\n")


def main(argv: list[str]) -> None:
    if argv and argv[0] == "--comment":
        path, sha = argv[1], argv[2]
        print(comment(Path(path).read_text(encoding="utf-8"), sha))
        return
    path, account, email = argv[0], argv[1], (argv[2] if len(argv) > 2 else "")
    p = Path(path)
    p.write_text(scrub(p.read_text(encoding="utf-8"), account, email), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
