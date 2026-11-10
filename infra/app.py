#!/usr/bin/env python3
"""CDK app for the dashboard's AWS hosting. See infra/README.md.

Two stacks; name the one you mean (cdk diff HipKneeDashboard, cdk deploy HipKneeDashboard):
  HipKneeDashboard     the site, data check and uploads (deployed by CI after approval)
  HipKneeDashboardCi   the GitHub OIDC roles CI deploys with (deployed rarely, from a laptop)

Settings (pass with -c name=value, or edit cdk.json):
  github_repo                  owner/name of the repository allowed to deploy
  github_branch                branch allowed to deploy (default: main)
  github_owner_id, github_repo_id
                               GitHub's permanent IDs for the owner and repository (see infra/README.md)
  existing_oidc_provider_arn   set if the account already has a GitHub OIDC provider
  alert_email                  email for data-check results, alarms and budget alerts (required;
                               -c allow_no_alerts=true only for a synth that won't be deployed)
  monthly_budget_usd           budget alert threshold, needs alert_email (default: 5)
  auth_domain_prefix           Cognito sign-in domain prefix, unique in the region
                               (default: hip-knee-dashboard-dito84)
"""

import sys

# The Lambda package pins numpy and pandas versions that need Python 3.12+, and pip checks
# that against the Python running this app. Stop here with a clear message instead of a pip error.
if sys.version_info < (3, 12):  # noqa: UP036 (a guard for people deploying from older Pythons)
    sys.exit(f"infra/ needs Python 3.12 or newer; this is {sys.version.split()[0]}. "
             "Recreate .venv with a newer Python (see 'Deploy it' in infra/README.md).")

import aws_cdk as cdk  # noqa: E402
from cdk_nag import AwsSolutionsChecks  # noqa: E402

from dashboard_infra.ci import CiStack  # noqa: E402
from dashboard_infra.nag import add_ci_suppressions, add_suppressions  # noqa: E402
from dashboard_infra.settings import alert_email  # noqa: E402
from dashboard_infra.stack import DashboardStack  # noqa: E402

app = cdk.App()
ctx = app.node.try_get_context
email = alert_email(ctx)   # stops here if it's missing: see dashboard_infra/settings.py

stack = DashboardStack(
    app, "HipKneeDashboard",
    github_repo=ctx("github_repo"),
    github_branch=ctx("github_branch") or "main",
    github_owner_id=str(ctx("github_owner_id") or "") or None,
    github_repo_id=str(ctx("github_repo_id") or "") or None,
    existing_oidc_provider_arn=ctx("existing_oidc_provider_arn") or None,
    alert_email=email,
    monthly_budget_usd=int(ctx("monthly_budget_usd") or 5),
    auth_domain_prefix=ctx("auth_domain_prefix") or "hip-knee-dashboard-dito84",
    env=cdk.Environment(region=ctx("region") or "us-east-1"),
    description="Hip & knee replacement outcomes dashboard: S3 + CloudFront site, GitHub OIDC deploy role, "
                "data-check Lambda, authenticated uploads",
    termination_protection=True,   # a mistaken `cdk destroy` fails instead of starting
)
ci_stack = CiStack(
    app, "HipKneeDashboardCi",
    github_repo=ctx("github_repo"),
    github_branch=ctx("github_branch") or "main",
    github_owner_id=str(ctx("github_owner_id") or "") or None,
    github_repo_id=str(ctx("github_repo_id") or "") or None,
    env=cdk.Environment(region=ctx("region") or "us-east-1"),
    description="Hip & knee replacement outcomes dashboard: GitHub OIDC roles for CI diffs, deploys and smoke tests",
    termination_protection=True,
)
for s in (stack, ci_stack):
    cdk.Tags.of(s).add("project", "hip-knee-replacement-outcomes-dashboard")

# Fail synth on any AWS Solutions security finding that isn't explained in nag.py
add_suppressions(stack)
add_ci_suppressions(ci_stack)
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
