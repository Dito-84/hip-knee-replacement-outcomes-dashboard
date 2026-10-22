#!/usr/bin/env python3
"""CDK app for the dashboard's AWS hosting. See infra/README.md.

Settings (pass with -c name=value, or edit cdk.json):
  github_repo                  owner/name of the repository allowed to deploy
  github_branch                branch allowed to deploy (default: main)
  existing_oidc_provider_arn   set if the account already has a GitHub OIDC provider
  alert_email                  email for data-check results and budget alerts (optional)
  monthly_budget_usd           budget alert threshold, needs alert_email (default: 5)
"""

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from dashboard_infra.nag import add_suppressions
from dashboard_infra.stack import DashboardStack

app = cdk.App()
ctx = app.node.try_get_context

stack = DashboardStack(
    app, "HipKneeDashboard",
    github_repo=ctx("github_repo"),
    github_branch=ctx("github_branch") or "main",
    existing_oidc_provider_arn=ctx("existing_oidc_provider_arn") or None,
    alert_email=ctx("alert_email") or None,
    monthly_budget_usd=int(ctx("monthly_budget_usd") or 5),
    env=cdk.Environment(region=ctx("region") or "us-east-1"),
    description="Hip & knee replacement outcomes dashboard: S3 + CloudFront site, GitHub OIDC deploy role, "
                "data-check Lambda",
)
cdk.Tags.of(stack).add("project", "hip-knee-replacement-outcomes-dashboard")

# Fail synth on any AWS Solutions security finding that isn't explained in nag.py
add_suppressions(stack)
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
