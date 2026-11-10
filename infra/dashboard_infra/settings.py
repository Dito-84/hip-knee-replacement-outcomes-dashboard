"""Settings read from CDK context that a deploy must not get wrong by leaving them out.

alert_email is not saved in cdk.json (the repository is public), so every synth passes it
with -c. Leaving it out used to synthesize the stack without the alert topic's email
subscription and the budget, and a deploy of that template removed both: every alarm and
the budget alert would stop emailing, with nothing to say so. Now a synth without it stops,
unless it says explicitly that it won't be deployed (CI's synth check).
"""

import re

EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class MissingAlertEmail(SystemExit):
    pass


def alert_email(ctx) -> str | None:
    """The alert email from context; None only with -c allow_no_alerts=true."""
    email = str(ctx("alert_email") or "").strip()
    if email:
        if not EMAIL.fullmatch(email):
            raise MissingAlertEmail("alert_email doesn't look like an email address.")
        return email
    if str(ctx("allow_no_alerts") or "").strip().lower() == "true":
        return None
    raise MissingAlertEmail(
        "No alert_email. Without it the stack has no alert email or budget, and deploying it would remove "
        "them. Pass -c alert_email=<address> (in CI, the ALERT_EMAIL secret), or -c allow_no_alerts=true "
        "for a synth that won't be deployed.")
