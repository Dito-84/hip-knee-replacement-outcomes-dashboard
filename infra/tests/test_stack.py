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
    kwargs.setdefault("github_owner_id", "142613285")
    kwargs.setdefault("github_repo_id", "1429165974")
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
    assert len(buckets) == 4   # logs, site, intake, reference
    for bucket in buckets.values():
        props = bucket["Properties"]
        assert props["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True, "BlockPublicPolicy": True,
            "IgnorePublicAcls": True, "RestrictPublicBuckets": True}
        assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0][
            "ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"


def test_every_bucket_rejects_plain_http(template):
    policies = template.find_resources("AWS::S3::BucketPolicy")
    assert len(policies) == 4
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
        "token.actions.githubusercontent.com:sub": [
            # Repos created after July 15, 2026 include GitHub's permanent owner and repository IDs
            "repo:Dito-84@142613285/hip-knee-replacement-outcomes-dashboard@1429165974:ref:refs/heads/main",
            f"repo:{REPO}:ref:refs/heads/main",
        ],
    }
    assert role["Properties"]["MaxSessionDuration"] == 3600


def test_without_ids_only_the_name_based_subject_is_trusted():
    template = Template.from_stack(make_stack(github_owner_id=None, github_repo_id=None))
    role = [r for k, r in template.find_resources("AWS::IAM::Role").items() if k.startswith("GitHubDeployRole")][0]
    condition = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["StringEquals"]
    assert condition["token.actions.githubusercontent.com:sub"] == [f"repo:{REPO}:ref:refs/heads/main"]


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
        "Environment": {"Variables": {"TOPIC_ARN": Match.any_value(), "REPORT_PREFIX": "reports/",
                                      "REFERENCE_BUCKET": Match.any_value(),
                                      "REFERENCE_KEY": "reference/discharges.csv"}},
    })


def test_only_uploads_trigger_the_check(template):
    template.has_resource_properties("Custom::S3BucketNotifications", {"NotificationConfiguration": {
        "LambdaFunctionConfigurations": [Match.object_like({
            "Events": ["s3:ObjectCreated:*"],
            "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "incoming/"}]}},
        })]}})


def test_data_check_role_reads_uploads_and_writes_reports_only(template):  # and reads the reference
    stmts = statements(template, "DataCheckRole")
    by_sid = {s.get("Sid"): s for s in stmts}
    assert by_sid["ReadUploads"]["Action"] == "s3:GetObject"
    assert by_sid["ReadUploads"]["Resource"]["Fn::Join"][1][-1] == "/incoming/*"
    # It deletes rejected files and checked website uploads, and nothing outside incoming/
    assert by_sid["DeleteUploads"]["Action"] == "s3:DeleteObject"
    assert by_sid["DeleteUploads"]["Resource"]["Fn::Join"][1][-1] == "/incoming/*"
    assert [s for s in stmts if "s3:DeleteObject" in str(s["Action"])] == [by_sid["DeleteUploads"]]
    assert by_sid["WriteReports"]["Action"] == "s3:PutObject"
    assert by_sid["WriteReports"]["Resource"]["Fn::Join"][1][-1] == "/reports/*"
    assert actions(stmts) <= {"s3:GetObject", "s3:PutObject", "s3:DeleteObject", "sns:Publish",
                              "kms:Decrypt", "kms:GenerateDataKey",                     # the alert topic's key
                              "sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl",  # failed events
                              "logs:CreateLogStream", "logs:PutLogEvents"}
    assert not [a for a in actions(stmts) if "*" in a], "no wildcard actions"
    # No AWS managed policies on the function's role
    role = [r for k, r in template.find_resources("AWS::IAM::Role").items() if k.startswith("DataCheckRole")][0]
    assert "ManagedPolicyArns" not in role["Properties"]


def _reference_bucket_id(template):
    [bucket_id] = [k for k in template.find_resources("AWS::S3::Bucket") if k.startswith("ReferenceBucket")]
    return bucket_id


def _mentions(value, logical_id):
    return logical_id in str(value)


def test_function_can_only_read_one_reference_object(template):
    """Read access to exactly reference/discharges.csv: no wildcard, no write, no delete."""
    ref = _reference_bucket_id(template)
    on_reference = [s for s in statements(template, "DataCheckRole") if _mentions(s["Resource"], ref)]
    assert len(on_reference) == 1
    [stmt] = on_reference
    assert stmt["Sid"] == "ReadReference" and stmt["Action"] == "s3:GetObject"
    assert stmt["Resource"]["Fn::Join"][1][-1] == "/reference/discharges.csv"
    assert "*" not in str(stmt["Resource"])


def test_reference_bucket_refuses_writes_from_the_function(template):
    ref = _reference_bucket_id(template)
    [policy] = [p for p in template.find_resources("AWS::S3::BucketPolicy").values()
                if p["Properties"]["Bucket"] == {"Ref": ref}]
    [deny] = [s for s in policy["Properties"]["PolicyDocument"]["Statement"]
              if s.get("Sid") == "DataCheckCannotChangeReference"]
    assert deny["Effect"] == "Deny" and _mentions(deny["Principal"], "DataCheckRole")
    assert {"s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"} <= set(deny["Action"])


def test_reference_is_versioned_and_keeps_recent_versions(template):
    bucket = template.find_resources("AWS::S3::Bucket")[_reference_bucket_id(template)]["Properties"]
    assert bucket["VersioningConfiguration"] == {"Status": "Enabled"}
    [rule] = bucket["LifecycleConfiguration"]["Rules"]
    assert rule["NoncurrentVersionExpiration"] == {"NewerNoncurrentVersions": 5, "NoncurrentDays": 365}
    assert bucket["LoggingConfiguration"]["LogFilePrefix"] == "s3/reference/"


# ---------- Failure alerting ----------

def _logical(template, type_, prefix):
    [found] = [k for k in template.find_resources(type_) if k.startswith(prefix)]
    return found


def test_failed_uploads_are_retried_then_kept_in_a_queue(template):
    queue = _logical(template, "AWS::SQS::Queue", "FailedChecks")
    template.has_resource_properties("AWS::Lambda::EventInvokeConfig", {
        "MaximumRetryAttempts": 2,
        "MaximumEventAgeInSeconds": 3600,
        "DestinationConfig": {"OnFailure": {"Destination": {"Fn::GetAtt": [queue, "Arn"]}}},
    })
    props = template.find_resources("AWS::SQS::Queue")[queue]["Properties"]
    assert props["SqsManagedSseEnabled"] is True
    assert props["MessageRetentionPeriod"] == 14 * 24 * 3600


def test_alarms_email_on_errors_and_on_given_up_uploads(template):
    topic = _logical(template, "AWS::SNS::Topic", "DataCheckAlerts")
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    # data check (errors, gave up) + upload functions (presign, results, pre-auth) + API 5xx;
    # 6 alarms, inside CloudWatch's free 10
    assert len(alarms) == 6
    metrics = sorted(a["Properties"]["MetricName"] for a in alarms.values())
    assert metrics == ["5xx", "ApproximateNumberOfMessagesVisible", "Errors", "Errors", "Errors", "Errors"]
    for props in alarms.values():
        props = props["Properties"]
        assert props["AlarmActions"] == [{"Ref": topic}]
        assert props["Threshold"] == 1 and props["TreatMissingData"] == "notBreaching"


def test_alert_topic_uses_a_key_cloudwatch_can_use(template):
    """CloudWatch alarms can't publish to a topic encrypted with the AWS managed alias/aws/sns key."""
    key = _logical(template, "AWS::KMS::Key", "AlertsKey")
    template.has_resource_properties("AWS::SNS::Topic", {"KmsMasterKeyId": {"Fn::GetAtt": [key, "Arn"]}})
    props = template.find_resources("AWS::KMS::Key")[key]["Properties"]
    assert props["EnableKeyRotation"] is True
    [stmt] = [s for s in props["KeyPolicy"]["Statement"] if s.get("Sid") == "CloudWatchAlarmsCanPublishToTheAlertTopic"]
    assert stmt["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert set(stmt["Action"]) == {"kms:Decrypt", "kms:GenerateDataKey"}
    assert "aws:SourceAccount" in stmt["Condition"]["StringEquals"]


def test_only_this_stacks_alarms_can_publish_to_the_topic(template):
    [policy] = template.find_resources("AWS::SNS::TopicPolicy").values()
    [stmt] = [s for s in policy["Properties"]["PolicyDocument"]["Statement"] if s.get("Sid") == "AlarmsCanPublish"]
    assert stmt["Action"] == "sns:Publish"
    arns = stmt["Condition"]["ArnLike"]["aws:SourceArn"]
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    assert sorted(str(a) for a in arns) == sorted(str({"Fn::GetAtt": [k, "Arn"]}) for k in alarms)


def test_uploads_and_reports_expire(template):
    template.has_resource_properties("AWS::S3::Bucket", {"LifecycleConfiguration": {"Rules": [
        Match.object_like({"Prefix": "incoming/", "ExpirationInDays": 7, "Status": "Enabled"}),
        Match.object_like({"Prefix": "reports/", "ExpirationInDays": 90, "Status": "Enabled"}),
        Match.object_like({"Prefix": "incoming/web/", "ExpirationInDays": 1, "Status": "Enabled"}),
        Match.object_like({"Prefix": "reports/web/", "ExpirationInDays": 7, "Status": "Enabled"}),
    ]}})


def test_intake_bucket_is_not_versioned(template):
    """A delete in a versioned bucket only adds a delete marker: a rejected file would still be stored."""
    [intake] = [b for k, b in template.find_resources("AWS::S3::Bucket").items() if k.startswith("IntakeBucket")]
    assert "VersioningConfiguration" not in intake["Properties"]


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
            Match.object_like(
                {"Notification": Match.object_like({"NotificationType": "FORECASTED", "Threshold": 100})}
            ),
        ],
    })


def test_outputs_give_github_what_it_needs(template):
    outputs = template.to_json()["Outputs"]
    for name in ("SiteUrl", "SiteBucketName", "DistributionId", "DeployRoleArn", "IntakeBucketName",
                 "ReferenceBucketName", "FailedChecksQueueUrl"):
        assert name in outputs


# ---------- Authenticated uploads ----------

def _one(template, kind):
    [(key, resource)] = template.find_resources(kind).items()
    return key, resource["Properties"]


def test_only_invited_users_can_sign_in(template):
    _, pool = _one(template, "AWS::Cognito::UserPool")
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    assert pool["UsernameAttributes"] == ["email"]
    assert pool["MfaConfiguration"] == "OPTIONAL" and pool["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]   # no SMS
    policy = pool["Policies"]["PasswordPolicy"]
    assert policy["MinimumLength"] == 12 and policy["TemporaryPasswordValidityDays"] == 7
    assert all(policy[k] for k in ("RequireLowercase", "RequireUppercase", "RequireNumbers", "RequireSymbols"))
    assert pool["UserPoolTier"] == "ESSENTIALS"
    assert pool["DeletionProtection"] == "ACTIVE"
    assert "PreAuthentication" in pool["LambdaConfig"]
    assert {"Name": "expires_at", "AttributeDataType": "String", "Mutable": True,
            "StringAttributeConstraints": {"MinLength": "20", "MaxLength": "25"}} in pool["Schema"]


def test_users_cant_write_any_attribute_so_cant_extend_their_expiry(template):
    _, client = _one(template, "AWS::Cognito::UserPoolClient")
    assert client["WriteAttributes"] == []
    assert "custom:expires_at" in client["ReadAttributes"]


def test_app_client_is_a_public_pkce_client_with_short_tokens(template):
    _, client = _one(template, "AWS::Cognito::UserPoolClient")
    assert client["GenerateSecret"] is False
    assert client["AllowedOAuthFlows"] == ["code"] and client["AllowedOAuthFlowsUserPoolClient"] is True
    assert set(client["AllowedOAuthScopes"]) == {"openid", "email"}
    assert client["SupportedIdentityProviders"] == ["COGNITO"]
    assert (client["AccessTokenValidity"], client["IdTokenValidity"], client["RefreshTokenValidity"]) == (30, 30, 60)
    assert client["TokenValidityUnits"] == {"AccessToken": "minutes", "IdToken": "minutes", "RefreshToken": "minutes"}
    assert client["EnableTokenRevocation"] is True and client["PreventUserExistenceErrors"] == "ENABLED"
    # Only the admin flow (smoke test; needs IAM credentials) besides OAuth: no password or SRP sign-in
    assert set(client["ExplicitAuthFlows"]) == {"ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"}
    for urls in (client["CallbackURLs"], client["LogoutURLs"]):
        [url] = urls
        assert url["Fn::Join"][1][-1] == "/upload.html"


def test_managed_login_on_a_cognito_domain(template):
    _, domain = _one(template, "AWS::Cognito::UserPoolDomain")
    assert domain["Domain"] == "hip-knee-dashboard-dito84" and domain["ManagedLoginVersion"] == 2
    _, branding = _one(template, "AWS::Cognito::ManagedLoginBranding")
    assert branding["UseCognitoProvidedValues"] is True


def test_every_api_route_needs_a_cognito_token(template):
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    assert sorted(r["Properties"]["RouteKey"] for r in routes.values()) == ["GET /uploads/{id}", "POST /uploads"]
    authorizer, props = _one(template, "AWS::ApiGatewayV2::Authorizer")
    assert props["AuthorizerType"] == "JWT"
    for route in routes.values():
        assert route["Properties"]["AuthorizationType"] == "JWT"
        assert route["Properties"]["AuthorizerId"] == {"Ref": authorizer}


def test_api_cors_allows_only_the_site(template):
    _, api = _one(template, "AWS::ApiGatewayV2::Api")
    cors = api["CorsConfiguration"]
    [origin] = cors["AllowOrigins"]
    assert origin["Fn::Join"][1][0] == "https://" and "Distribution" in str(origin)
    assert set(cors["AllowMethods"]) == {"GET", "POST"}


def test_api_is_throttled_and_logged(template):
    _, stage = _one(template, "AWS::ApiGatewayV2::Stage")
    assert stage["DefaultRouteSettings"] == {"ThrottlingBurstLimit": 10, "ThrottlingRateLimit": 5}
    assert stage["RouteSettings"]["POST /uploads"] == {"ThrottlingBurstLimit": 2, "ThrottlingRateLimit": 1}
    assert "AccessLogSettings" in stage and "$context.authorizer.claims.sub" in stage["AccessLogSettings"]["Format"]


def test_presign_role_can_only_sign_web_uploads_and_count_them(template):
    stmts = statements(template, "UploadsPresignFunctionRole")
    by_sid = {s.get("Sid"): s for s in stmts}
    assert by_sid["SignWebUploads"]["Action"] == "s3:PutObject"
    assert by_sid["SignWebUploads"]["Resource"]["Fn::Join"][1][-1] == "/incoming/web/*"
    assert by_sid["CountUploads"]["Action"] == "dynamodb:UpdateItem"
    assert "UploadQuota" in str(by_sid["CountUploads"]["Resource"])
    assert actions(stmts) == {"s3:PutObject", "dynamodb:UpdateItem", "logs:CreateLogStream", "logs:PutLogEvents"}


def test_results_role_can_only_read_web_results(template):
    stmts = statements(template, "UploadsResultsFunctionRole")
    by_sid = {s.get("Sid"): s for s in stmts}
    assert by_sid["ReadWebResults"]["Action"] == "s3:GetObject"
    assert by_sid["ReadWebResults"]["Resource"]["Fn::Join"][1][-1] == "/reports/web/*"
    assert actions(stmts) == {"s3:GetObject", "logs:CreateLogStream", "logs:PutLogEvents"}


def test_pre_auth_trigger_has_no_aws_permissions(template):
    assert actions(statements(template, "UploadsPreAuthFunctionRole")) == {"logs:CreateLogStream",
                                                                            "logs:PutLogEvents"}


def test_no_upload_function_uses_aws_managed_policies(template):
    roles = {k: r for k, r in template.find_resources("AWS::IAM::Role").items() if k.startswith("Uploads")}
    assert len(roles) == 3
    assert all("ManagedPolicyArns" not in r["Properties"] for r in roles.values())


def test_only_the_presign_role_can_write_web_uploads(template):
    intake = [k for k in template.find_resources("AWS::S3::Bucket") if k.startswith("IntakeBucket")][0]
    policies = [p["Properties"] for p in template.find_resources("AWS::S3::BucketPolicy").values()
                if p["Properties"]["Bucket"] == {"Ref": intake}]
    [stmt] = [s for p in policies for s in p["PolicyDocument"]["Statement"]
              if s.get("Sid") == "OnlyThePresignRoleWritesWebUploads"]
    assert stmt["Effect"] == "Deny" and stmt["Action"] == "s3:PutObject" and stmt["Principal"] == {"AWS": "*"}
    assert stmt["Resource"]["Fn::Join"][1][-1] == "/incoming/web/*"
    assert "UploadsPresignFunctionRole" in str(stmt["Condition"]["ArnNotEquals"]["aws:PrincipalArn"])


def test_intake_bucket_cors_allows_only_posts_from_the_site(template):
    [intake] = [b for k, b in template.find_resources("AWS::S3::Bucket").items() if k.startswith("IntakeBucket")]
    [rule] = intake["Properties"]["CorsConfiguration"]["CorsRules"]
    assert rule["AllowedMethods"] == ["POST"]
    [origin] = rule["AllowedOrigins"]
    assert origin["Fn::Join"][1][0] == "https://" and "Distribution" in str(origin)


def test_upload_limits_table(template):
    _, table = _one(template, "AWS::DynamoDB::Table")
    assert table["BillingMode"] == "PAY_PER_REQUEST"
    assert table["TimeToLiveSpecification"] == {"AttributeName": "expires", "Enabled": True}
    assert table["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}]


def test_upload_page_outputs(template):
    outputs = template.to_json()["Outputs"]
    assert {"UploadPageUrl", "UploadApiUrl", "UserPoolId", "UserPoolClientId", "AuthDomain"} <= set(outputs)
