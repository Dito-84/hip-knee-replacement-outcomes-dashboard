"""The CI stack: who can assume each GitHub OIDC role, and exactly what each can do."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Annotations, Match, Template
from cdk_nag import AwsSolutionsChecks

from dashboard_infra.ci import CiStack, oidc_subjects
from dashboard_infra.nag import add_ci_suppressions

REPO = "Dito-84/hip-knee-replacement-outcomes-dashboard"
IDS = ("142613285", "1429165974")


@pytest.fixture(scope="module")
def stack():
    app = cdk.App()
    stack = CiStack(app, "Ci", github_repo=REPO, github_owner_id=IDS[0], github_repo_id=IDS[1],
                    env=cdk.Environment(region="us-east-1"))
    add_ci_suppressions(stack)
    cdk.Aspects.of(app).add(AwsSolutionsChecks())
    return stack


@pytest.fixture(scope="module")
def template(stack):
    return Template.from_stack(stack)


def role(template, prefix):
    [(key, resource)] = [(k, r) for k, r in template.find_resources("AWS::IAM::Role").items() if k.startswith(prefix)]
    return key, resource["Properties"]


def statements(template, prefix):
    key, _ = role(template, prefix)
    found = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        if {"Ref": key} in policy["Properties"]["Roles"]:
            found += policy["Properties"]["PolicyDocument"]["Statement"]
    return found


def actions(stmts):
    return {a for s in stmts for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}


def subjects(template, prefix):
    _, props = role(template, prefix)
    [stmt] = props["AssumeRolePolicyDocument"]["Statement"]
    assert stmt["Action"] == "sts:AssumeRoleWithWebIdentity"
    cond = stmt["Condition"]["StringEquals"]
    assert cond["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    subs = cond["token.actions.githubusercontent.com:sub"]
    return set([subs] if isinstance(subs, str) else subs)


def test_no_unexplained_security_findings(stack):
    errors = Annotations.from_stack(stack).find_error("*", Match.string_like_regexp("AwsSolutions-.*"))
    assert errors == [], [e.entry.data for e in errors]


def test_subjects_cover_both_github_formats():
    assert oidc_subjects(REPO, ["pull_request"], *IDS) == [
        "repo:Dito-84@142613285/hip-knee-replacement-outcomes-dashboard@1429165974:pull_request",
        f"repo:{REPO}:pull_request",
    ]
    assert oidc_subjects(REPO, ["pull_request"], None, None) == [f"repo:{REPO}:pull_request"]


def test_diff_role_is_for_pull_requests_and_main_only(template):
    assert subjects(template, "DiffRole") == set(
        oidc_subjects(REPO, ["pull_request", "ref:refs/heads/main"], *IDS))


def test_deploy_and_smoke_roles_need_the_production_environment(template):
    """Only jobs in the `production` environment, which waits for a required reviewer, get these."""
    expected = set(oidc_subjects(REPO, ["environment:production"], *IDS))
    assert subjects(template, "DeployRole") == expected
    assert subjects(template, "SmokeRole") == expected


def test_diff_role_is_read_only_on_the_two_stacks(template):
    stmts = statements(template, "DiffRole")
    assert actions(stmts) == {"cloudformation:DescribeStacks", "cloudformation:GetTemplate",
                              "cloudformation:GetTemplateSummary"}
    [stmt] = stmts
    resources = [r["Fn::Join"][1][-1] for r in stmt["Resource"]]
    assert resources == [":stack/HipKneeDashboard/*", ":stack/HipKneeDashboardCi/*"]


def test_deploy_role_can_only_use_cdk_roles_and_this_stacks_change_sets(template):
    stmts = {s["Sid"]: s for s in statements(template, "DeployRole")}
    assume = stmts["UseCdkBootstrapRoles"]
    assert set(assume["Action"]) == {"sts:AssumeRole", "sts:TagSession"}
    targets = [str(r) for r in assume["Resource"]]
    assert len(targets) == 2
    assert any("cdk-hnb659fds-deploy-role-" in t for t in targets)
    assert any("cdk-hnb659fds-file-publishing-role-" in t for t in targets)
    assert not any("lookup" in t or "image-publishing" in t for t in targets)
    change_sets = stmts["InspectAndExecuteChangeSets"]
    assert change_sets["Resource"]["Fn::Join"][1][-1] == ":stack/HipKneeDashboard/*"
    assert "cloudformation:CreateChangeSet" not in change_sets["Action"]   # only through CDK's deploy role
    assert not [a for a in actions(stmts.values()) if a.startswith("iam:") or a.endswith("*")]


def test_smoke_role_only_has_what_the_smoke_test_uses(template):
    stmts = {s["Sid"]: s for s in statements(template, "SmokeRole")}
    assert actions(stmts.values()) == {
        "cloudformation:DescribeStacks", "cognito-idp:AdminCreateUser", "cognito-idp:AdminSetUserPassword",
        "cognito-idp:AdminInitiateAuth", "cognito-idp:AdminDeleteUser", "s3:ListBucket", "s3:GetObject"}
    assert stmts["SeeWebUploadsAreGone"]["Condition"] == {"StringLike": {"s3:prefix": ["incoming/web/*"]}}
    assert stmts["HeadWebUploads"]["Resource"] == "arn:aws:s3:::hipkneedashboard-intakebucket*/incoming/web/*"


def test_roles_have_one_hour_sessions_and_no_managed_policies(template):
    for prefix in ("DiffRole", "DeployRole", "SmokeRole"):
        _, props = role(template, prefix)
        assert props["MaxSessionDuration"] == 3600
        assert "ManagedPolicyArns" not in props


def test_outputs_name_the_github_variables(template):
    outputs = template.to_json()["Outputs"]
    assert {o["Description"] for o in outputs.values()} == {
        "GitHub variable AWS_DIFF_ROLE_ARN", "GitHub variable AWS_STACK_DEPLOY_ROLE_ARN",
        "GitHub variable AWS_SMOKE_ROLE_ARN"}


def test_the_ci_stack_creates_no_oidc_provider(template):
    """It reuses the one the main stack created; a second provider for the same URL would fail to deploy."""
    assert template.find_resources("Custom::AWSCDKOpenIdConnectProvider") == {}
    assert template.find_resources("AWS::IAM::OIDCProvider") == {}
