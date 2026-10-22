"""Checks on the CloudFormation template the CDK app produces.

Bundling is skipped (aws:cdk:bundling-stacks = []), so these run in seconds
without downloading pandas.
"""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Annotations, Match, Template
from cdk_nag import AwsSolutionsChecks

from dashboard_infra.nag import add_suppressions
from dashboard_infra.stack import DashboardStack

REPO = "Dito-84/hip-knee-replacement-outcomes-dashboard"


def make_stack(**kwargs):
    app = cdk.App(context={"aws:cdk:bundling-stacks": []})
    stack = DashboardStack(app, "Test", github_repo=REPO, env=cdk.Environment(region="us-east-1"), **kwargs)
    add_suppressions(stack)
    cdk.Aspects.of(app).add(AwsSolutionsChecks())
    return stack


@pytest.fixture(scope="module")
def stack():
    return make_stack()


@pytest.fixture(scope="module")
def template(stack):
    return Template.from_stack(stack)


def statements(template, role_logical_prefix):
    """All IAM statements attached to the role whose logical ID starts with the prefix."""
    found = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        roles = [r["Ref"] for r in policy["Properties"]["Roles"]]
        if any(r.startswith(role_logical_prefix) for r in roles):
            found += policy["Properties"]["PolicyDocument"]["Statement"]
    return found


def actions(stmts):
    out = set()
    for s in stmts:
        out.update([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
    return out


# ---------- Security baseline ----------

def test_no_unexplained_security_findings(stack):
    for level in ("find_error", "find_warning"):
        findings = getattr(Annotations.from_stack(stack), level)("*", Match.string_like_regexp("AwsSolutions-.*"))
        assert findings == [], "\n".join(f"{f.id}: {f.entry.data}" for f in findings)


def test_every_bucket_blocks_public_access_and_encrypts(template):
    buckets = template.find_resources("AWS::S3::Bucket")
    assert len(buckets) == 3
    for bucket in buckets.values():
        props = bucket["Properties"]
        assert props["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True, "BlockPublicPolicy": True,
            "IgnorePublicAcls": True, "RestrictPublicBuckets": True}
        assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0][
            "ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"


def test_every_bucket_rejects_plain_http(template):
    policies = template.find_resources("AWS::S3::BucketPolicy")
    assert len(policies) == 3
    for policy in policies.values():
        denies = [s for s in policy["Properties"]["PolicyDocument"]["Statement"] if s["Effect"] == "Deny"]
        assert any(s["Condition"] == {"Bool": {"aws:SecureTransport": "false"}} for s in denies)


def test_buckets_are_kept_if_the_stack_is_deleted(template):
    for bucket in template.find_resources("AWS::S3::Bucket").values():
        assert bucket["DeletionPolicy"] == "Retain"


# ---------- Static site ----------

def test_site_is_served_only_through_cloudfront(template):
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
    template.has_resource_properties("AWS::CloudFront::Distribution", {"DistributionConfig": Match.object_like({
        "DefaultRootObject": "index.html",
        "PriceClass": "PriceClass_100",
        "Logging": Match.object_like({"Prefix": "cloudfront/"}),
        "DefaultCacheBehavior": Match.object_like({
            "ViewerProtocolPolicy": "redirect-to-https",
            "ResponseHeadersPolicyId": Match.any_value(),
        }),
    })})
    # The site bucket grants read only to the CloudFront service, for this distribution
    site_policy = [p for p in template.find_resources("AWS::S3::BucketPolicy").values()
                   if p["Properties"]["Bucket"]["Ref"].startswith("SiteBucket")][0]
    allows = [s for s in site_policy["Properties"]["PolicyDocument"]["Statement"] if s["Effect"] == "Allow"]
    assert len(allows) == 1
    assert allows[0]["Principal"] == {"Service": "cloudfront.amazonaws.com"}
    assert allows[0]["Action"] == "s3:GetObject"
    assert "AWS:SourceArn" in allows[0]["Condition"]["StringEquals"]


def test_site_bucket_is_versioned_for_rollback(template):
    template.has_resource_properties("AWS::S3::Bucket", {
        "VersioningConfiguration": {"Status": "Enabled"},
        "LoggingConfiguration": {"DestinationBucketName": Match.any_value(), "LogFilePrefix": "s3/site/"},
    })


# ---------- GitHub deploy role ----------

def test_only_main_branch_of_this_repo_can_assume_the_deploy_role(template):
    role = [r for k, r in template.find_resources("AWS::IAM::Role").items() if k.startswith("GitHubDeployRole")][0]
    [statement] = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert statement["Condition"]["StringEquals"] == {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": f"repo:{REPO}:ref:refs/heads/main",
    }
    assert role["Properties"]["MaxSessionDuration"] == 3600


def test_deploy_role_can_only_sync_files_and_invalidate_the_cache(template):
    assert actions(statements(template, "GitHubDeployRole")) == {
        "s3:ListBucket", "s3:PutObject", "s3:DeleteObject",
        "cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"}


def test_github_oidc_provider_is_created_once(template):
    template.resource_count_is("AWS::IAM::OIDCProvider", 1)
    template.has_resource_properties("AWS::IAM::OIDCProvider", {
        "Url": "https://token.actions.githubusercontent.com", "ClientIdList": ["sts.amazonaws.com"]})


def test_existing_oidc_provider_can_be_reused():
    arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    template = Template.from_stack(make_stack(existing_oidc_provider_arn=arn))
    template.resource_count_is("AWS::IAM::OIDCProvider", 0)
    role = [r for k, r in template.find_resources("AWS::IAM::Role").items() if k.startswith("GitHubDeployRole")][0]
    assert role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {"Federated": arn}


# ---------- Data check ----------

def test_data_check_function_settings(template):
    template.has_resource_properties("AWS::Lambda::Function", {
        "Handler": "handler.handler",
        "Runtime": "python3.14",
        "Architectures": ["arm64"],
        "MemorySize": 1024,
        "Timeout": 120,
        "Environment": {"Variables": {"TOPIC_ARN": Match.any_value(), "REPORT_PREFIX": "reports/"}},
    })


def test_only_uploads_trigger_the_check(template):
    template.has_resource_properties("Custom::S3BucketNotifications", {"NotificationConfiguration": {
        "LambdaFunctionConfigurations": [Match.object_like({
            "Events": ["s3:ObjectCreated:*"],
            "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "incoming/"}]}},
        })]}})


def test_data_check_role_reads_uploads_and_writes_reports_only(template):
    stmts = statements(template, "DataCheckRole")
    by_sid = {s.get("Sid"): s for s in stmts}
    assert by_sid["ReadUploads"]["Action"] == "s3:GetObject"
    assert by_sid["ReadUploads"]["Resource"]["Fn::Join"][1][-1] == "/incoming/*"
    assert by_sid["WriteReports"]["Action"] == "s3:PutObject"
    assert by_sid["WriteReports"]["Resource"]["Fn::Join"][1][-1] == "/reports/*"
    assert actions(stmts) <= {"s3:GetObject", "s3:PutObject", "sns:Publish",
                              "logs:CreateLogStream", "logs:PutLogEvents"}
    # No AWS managed policies on the function's role
    role = [r for k, r in template.find_resources("AWS::IAM::Role").items() if k.startswith("DataCheckRole")][0]
    assert "ManagedPolicyArns" not in role["Properties"]


def test_uploads_and_reports_expire(template):
    template.has_resource_properties("AWS::S3::Bucket", {"LifecycleConfiguration": {"Rules": [
        Match.object_like({"Prefix": "incoming/", "ExpirationInDays": 7, "Status": "Enabled"}),
        Match.object_like({"Prefix": "reports/", "ExpirationInDays": 90, "Status": "Enabled"}),
    ]}})


def test_alert_topic_is_encrypted_and_email_is_optional(template):
    template.has_resource_properties("AWS::SNS::Topic", {"KmsMasterKeyId": Match.any_value()})
    template.resource_count_is("AWS::SNS::Subscription", 0)
    template.resource_count_is("AWS::Budgets::Budget", 0)
    with_email = Template.from_stack(make_stack(alert_email="me@example.com"))
    with_email.has_resource_properties("AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "me@example.com"})


def test_budget_alert_comes_with_an_email():
    template = Template.from_stack(make_stack(alert_email="me@example.com", monthly_budget_usd=10))
    template.has_resource_properties("AWS::Budgets::Budget", {
        "Budget": {"BudgetType": "COST", "TimeUnit": "MONTHLY", "BudgetLimit": {"Amount": 10, "Unit": "USD"}},
        "NotificationsWithSubscribers": [
            Match.object_like({"Notification": Match.object_like({"NotificationType": "ACTUAL", "Threshold": 80})}),
            Match.object_like({"Notification": Match.object_like({"NotificationType": "FORECASTED", "Threshold": 100})}),
        ],
    })


def test_outputs_give_github_what_it_needs(template):
    outputs = template.to_json()["Outputs"]
    for name in ("SiteUrl", "SiteBucketName", "DistributionId", "DeployRoleArn", "IntakeBucketName"):
        assert name in outputs
