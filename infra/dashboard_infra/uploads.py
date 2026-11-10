"""Authenticated uploads: sign-in, an upload API and its limits.

A reviewer you invite signs in through Cognito's managed login on the site's
/upload.html page, asks the API for a presigned S3 POST, and uploads a synthetic
CSV straight to the intake bucket under incoming/web/<their sub>/. The existing
data-check Lambda (synthetic-only guard first) checks it and deletes it; the page
polls the API for the counts-only result.

* Cognito user pool: no self sign-up, admin-created users only, optional TOTP MFA
  (no SMS), Essentials tier for managed login. A pre-authentication trigger refuses
  sign-in after the account's custom:valid_until, which is immutable: set when the
  account is created and never changeable after, by the user or anyone else. The app
  client is public (PKCE, no secret) and can write only email.
* HTTP API: a Cognito JWT authorizer on every route, CORS for the site's origin only,
  throttling, access logs. POST /uploads signs an upload; GET /uploads/{id} returns
  a result.
* DynamoDB: per-user and all-users daily upload counters, which HTTP APIs can't do
  themselves (no usage plans).
* The intake bucket: CORS for the site's origin, and a policy that lets only the
  presign function's role write under incoming/web/.
"""

import json
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk.aws_apigatewayv2_authorizers import HttpUserPoolAuthorizer
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct

from .bundling import ARCHITECTURE, RUNTIME

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "lambda"
UPLOAD_PAGE = "upload.html"
WEB_UPLOADS = "incoming/web/*"
WEB_REPORTS = "reports/web/*"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
PRESIGN_EXPIRES_SECONDS = 300
PER_USER_DAILY = 20
ALL_USERS_DAILY = 200
TOKEN_MINUTES = 30
CLEANUP_GRACE_DAYS = 7   # expired accounts are deleted this long after their valid_until


class Uploads(Construct):
    def __init__(self, scope: Construct, id: str, *, intake_bucket: s3.Bucket, site_domain: str,
                 auth_domain_prefix: str) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.site_origin = f"https://{site_domain}"
        page_url = f"{self.site_origin}/{UPLOAD_PAGE}"

        # ---------- Who can sign in ----------
        self.pre_auth = self._function(
            "PreAuthFunction", "pre_auth", "handler.handler",
            "Refuses sign-in to demo accounts past their custom:valid_until")

        self.user_pool = cognito.UserPool(
            self, "UserPool",
            user_pool_name="hip-knee-dashboard-reviewers",
            self_sign_up_enabled=False,                       # you create every account
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True)),
            custom_attributes={
                # The account's end date. Immutable: only set at creation, so a signed-in user can't
                # extend it with their own token (UpdateUserAttributes). To extend an account, recreate it.
                "valid_until": cognito.StringAttribute(min_len=20, max_len=25, mutable=False),
                # Retired (#33): the first expiry attribute was mutable, and an app client's empty
                # write list turned out to mean "everything writable". Cognito can't remove or change
                # a custom attribute, so it stays in the schema, unread and unwritable.
                "expires_at": cognito.StringAttribute(min_len=20, max_len=25, mutable=True),
            },
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),   # no SMS: no cost, no sandbox
            password_policy=cognito.PasswordPolicy(
                min_length=12, require_lowercase=True, require_uppercase=True, require_digits=True,
                require_symbols=True, temp_password_validity=Duration.days(7)),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            feature_plan=cognito.FeaturePlan.ESSENTIALS,    # managed login; free under 10,000 users a month
            user_invitation=cognito.UserInvitationConfig(
                email_subject="Your invitation to the Hip & Knee Dashboard demo",
                email_body=(
                    "You've been invited to try the upload demo of the Hip &amp; Knee Replacement Outcomes "
                    f"Dashboard.<br><br>Sign in at {page_url}<br>Email: {{username}}<br>"
                    "Temporary password: {####}<br><br>You'll choose your own password when you first sign "
                    "in. The temporary password expires in 7 days, and the account itself expires on the "
                    "date you were told. Only upload the synthetic sample files from that page."),
            ),
            # Pre-authentication runs on sign-in only. Pre-token-generation also runs when a refresh token
            # is used, so a refresh token captured from the token response can't outlive the account
            lambda_triggers=cognito.UserPoolTriggers(pre_authentication=self.pre_auth,
                                                     pre_token_generation=self.pre_auth),
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.domain = self.user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=auth_domain_prefix),
            managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
        )
        self.client = self.user_pool.add_client(
            "WebClient",
            user_pool_client_name="upload-page",
            generate_secret=False,                            # a browser page can't keep a secret: PKCE instead
            # SRP: the password method sign-in pages use, and Cognito's default when none is set.
            # Admin password auth is for the smoke test only and needs IAM credentials.
            auth_flows=cognito.AuthFlow(user_srp=True, admin_user_password=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[page_url],
                logout_urls=[page_url],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            access_token_validity=Duration.minutes(TOKEN_MINUTES),
            id_token_validity=Duration.minutes(TOKEN_MINUTES),
            refresh_token_validity=Duration.minutes(60),      # Cognito's minimum; the page discards it
            enable_token_revocation=True,
            prevent_user_existence_errors=True,
            # The email scope needs read access to email AND email_verified: without email_verified,
            # Cognito answers every code exchange with invalid_grant (token endpoint documentation)
            read_attributes=cognito.ClientAttributes().with_standard_attributes(email=True, email_verified=True)
                                                       .with_custom_attributes("valid_until"),
            # An explicit list: Cognito treats an empty WriteAttributes as unset, which makes every
            # attribute writable (found by scripts/smoke_upload.py). Email is required, so it stays.
            write_attributes=cognito.ClientAttributes().with_standard_attributes(email=True),
        )
        cognito.CfnManagedLoginBranding(
            self, "ManagedLoginStyle",
            user_pool_id=self.user_pool.user_pool_id,
            client_id=self.client.user_pool_client_id,
            use_cognito_provided_values=True,
        )

        # ---------- Daily upload limits ----------
        self.quota = dynamodb.Table(
            self, "UploadQuota",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires",                 # counters remove themselves after 2 days
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.DESTROY,             # only counters: nothing to keep
        )

        # ---------- The two API functions, each with only what it needs ----------
        api_env = {"BUCKET": intake_bucket.bucket_name}
        self.presign = self._function(
            "PresignFunction", "upload_api", "presign.handler",
            "Signs one upload of a synthetic CSV to incoming/web/<user>/ (5 minutes, 5 MB)",
            environment={**api_env, "TABLE": self.quota.table_name, "MAX_BYTES": str(MAX_UPLOAD_BYTES),
                         "EXPIRES_IN": str(PRESIGN_EXPIRES_SECONDS), "PER_USER_DAILY": str(PER_USER_DAILY),
                         "ALL_USERS_DAILY": str(ALL_USERS_DAILY)})
        self.presign.role.add_to_policy(iam.PolicyStatement(
            sid="SignWebUploads", actions=["s3:PutObject"],
            resources=[intake_bucket.arn_for_objects(WEB_UPLOADS)]))
        self.presign.role.add_to_policy(iam.PolicyStatement(
            sid="CountUploads", actions=["dynamodb:UpdateItem"], resources=[self.quota.table_arn]))

        self.results = self._function(
            "ResultsFunction", "upload_api", "results.handler",
            "Returns the counts-only result of one of the caller's own uploads", environment=api_env)
        self.results.role.add_to_policy(iam.PolicyStatement(
            sid="ReadWebResults", actions=["s3:GetObject"],
            resources=[intake_bucket.arn_for_objects(WEB_REPORTS)]))

        # ---------- The intake bucket, for browser uploads ----------
        intake_bucket.add_cors_rule(
            allowed_methods=[s3.HttpMethods.POST], allowed_origins=[self.site_origin],
            allowed_headers=["*"], max_age=3600)
        # Belt and braces: whatever another role is granted, only the presign role (whose
        # signature every website upload carries) can write under incoming/web/
        intake_bucket.add_to_resource_policy(iam.PolicyStatement(
            sid="OnlyThePresignRoleWritesWebUploads", effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()], actions=["s3:PutObject"],
            resources=[intake_bucket.arn_for_objects(WEB_UPLOADS)],
            conditions={"ArnNotEquals": {"aws:PrincipalArn": self.presign.role.role_arn}}))

        # ---------- The API ----------
        access_logs = logs.LogGroup(self, "ApiAccessLogs", retention=logs.RetentionDays.ONE_MONTH,
                                    removal_policy=RemovalPolicy.DESTROY)
        self.api = apigw.HttpApi(
            self, "Api",
            api_name="hip-knee-dashboard-uploads",
            description="Signed-in uploads of synthetic CSVs for the data check",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=[self.site_origin],
                allow_methods=[apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.hours(1)),
            default_authorizer=HttpUserPoolAuthorizer(
                "Cognito", self.user_pool, user_pool_clients=[self.client]),
        )
        [post_route] = self.api.add_routes(path="/uploads", methods=[apigw.HttpMethod.POST],
                                           integration=HttpLambdaIntegration("PresignIntegration", self.presign))
        self.api.add_routes(path="/uploads/{id}", methods=[apigw.HttpMethod.GET],
                            integration=HttpLambdaIntegration("ResultsIntegration", self.results))
        stage: apigw.CfnStage = self.api.default_stage.node.default_child
        stage.default_route_settings = apigw.CfnStage.RouteSettingsProperty(
            throttling_burst_limit=10, throttling_rate_limit=5)
        stage.route_settings = {"POST /uploads": {"ThrottlingBurstLimit": 2, "ThrottlingRateLimit": 1}}
        # A stage's route settings must name a route that already exists, and CloudFormation
        # would otherwise create the stage first: make it wait for the route
        stage.add_resource_dependency(post_route.node.default_child)
        stage.access_log_settings = apigw.CfnStage.AccessLogSettingsProperty(
            destination_arn=access_logs.log_group_arn,
            format=json.dumps({
                "requestId": "$context.requestId", "time": "$context.requestTime",
                "routeKey": "$context.routeKey", "status": "$context.status",
                "user": "$context.authorizer.claims.sub", "ip": "$context.identity.sourceIp",
                "latencyMs": "$context.responseLatency", "authError": "$context.authorizer.error",
            }))

        # ---------- Clean-up: expired accounts don't keep a reviewer's email address ----------
        self.cleanup = self._function(
            "AccountCleanupFunction", "account_cleanup", "handler.handler",
            f"Deletes demo accounts {CLEANUP_GRACE_DAYS} days after their custom:valid_until",
            environment={"USER_POOL_ID": self.user_pool.user_pool_id, "GRACE_DAYS": str(CLEANUP_GRACE_DAYS)},
            timeout=Duration.minutes(1))
        self.cleanup.add_to_role_policy(iam.PolicyStatement(
            sid="DeleteExpiredAccounts", actions=["cognito-idp:ListUsers", "cognito-idp:AdminDeleteUser"],
            resources=[self.user_pool.user_pool_arn]))
        events.Rule(self, "DailyCleanup", description="Deletes demo accounts a week after they expire",
                    schedule=events.Schedule.cron(minute="17", hour="7"),   # 2:17 am New York in winter
                    targets=[targets.LambdaFunction(self.cleanup, retry_attempts=2)])

        # ---------- Alarms (the stack wires them to the alert topic) ----------
        self.alarms = [
            self._error_alarm("PresignErrors", self.presign, "The upload-signing function failed."),
            self._error_alarm("ResultsErrors", self.results, "The upload-results function failed."),
            self._error_alarm("CleanupErrors", self.cleanup, "The daily account clean-up failed."),
            self._pre_auth_crash_alarm(),
            cloudwatch.Alarm(
                self, "ApiServerErrors",
                alarm_description="The upload API returned 5xx errors.",
                metric=self.api.metric_server_error(period=Duration.minutes(5), statistic="Sum"),
                threshold=1, evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING),
        ]

        # ---------- Outputs: what the upload page and the scripts need ----------
        CfnOutput(stack, "UploadPageUrl", value=page_url)
        CfnOutput(stack, "UploadApiUrl", value=self.api.api_endpoint, description="GitHub variable UPLOAD_API_URL")
        CfnOutput(stack, "UserPoolId", value=self.user_pool.user_pool_id,
                  description="Used by scripts/demo_user.py and scripts/smoke_upload.py")
        CfnOutput(stack, "UserPoolClientId", value=self.client.user_pool_client_id,
                  description="GitHub variable UPLOAD_CLIENT_ID")
        CfnOutput(stack, "AuthDomain", value=self.domain.base_url(),
                  description="GitHub variable UPLOAD_AUTH_DOMAIN")

    def _function(self, id: str, folder: str, handler: str, description: str,
                  environment: dict | None = None, timeout: Duration | None = None) -> lambda_.Function:
        """A small function with its own role and log group, and no AWS managed policies."""
        log_group = logs.LogGroup(self, f"{id}Logs", retention=logs.RetentionDays.ONE_MONTH,
                                  removal_policy=RemovalPolicy.DESTROY)
        role = iam.Role(self, f"{id}Role", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
        log_group.grant_write(role)
        return lambda_.Function(
            self, id, description=description, runtime=RUNTIME, architecture=ARCHITECTURE,
            handler=handler, code=lambda_.Code.from_asset(str(LAMBDA_DIR / folder)),
            memory_size=256, timeout=timeout or Duration.seconds(10), role=role, log_group=log_group,
            environment=environment or {})

    def _pre_auth_crash_alarm(self) -> cloudwatch.Alarm:
        """Alarm when the expiry trigger crashes, but not when it refuses an expired account.

        Cognito's only way to refuse a sign-in is an exception from the trigger, which Lambda counts
        in its Errors metric, so an alarm on Errors would email on every expired sign-in. Instead a
        metric filter counts logged errors other than AccountExpired.
        """
        metric_filter = self.pre_auth.log_group.add_metric_filter(
            "UnexpectedErrors",
            filter_pattern=logs.FilterPattern.literal('"[ERROR]" -AccountExpired'),
            metric_namespace="HipKneeDashboard",
            metric_name="PreAuthUnexpectedErrors",
            metric_value="1",
        )
        return cloudwatch.Alarm(
            self, "PreAuthErrors",
            alarm_description="The sign-in expiry trigger crashed (refusing an expired account doesn't count).",
            metric=metric_filter.metric(period=Duration.minutes(5), statistic="Sum"),
            threshold=1, evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING)

    def _error_alarm(self, id: str, fn: lambda_.Function, description: str) -> cloudwatch.Alarm:
        return cloudwatch.Alarm(
            self, id, alarm_description=description,
            metric=fn.metric_errors(period=Duration.minutes(5), statistic="Sum"),
            threshold=1, evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING)
