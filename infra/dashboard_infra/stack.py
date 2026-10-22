"""AWS hosting for the hip & knee replacement outcomes dashboard.

Three parts:

* Static site: a private S3 bucket served only through CloudFront (Origin
  Access Control), HTTPS-only, with security headers and access logs.
* Deploy role: GitHub Actions assumes this role through OIDC, so no AWS keys
  are stored in GitHub. Only the main branch of this one repository can assume
  it, and it can only update this site's bucket and invalidate its CDN cache.
* Data check: uploading a discharge file to the intake bucket under incoming/
  runs the dashboard's data-quality checks in Lambda and emails a summary.
"""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

from .bundling import ARCHITECTURE, RUNTIME, data_check_code

GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"


def github_subjects(repo: str, branch: str, owner_id: str | None, repo_id: str | None) -> list[str]:
    """The OIDC `sub` values GitHub can send for pushes to `branch` of `repo`.

    Repositories created, renamed or transferred after July 15, 2026 identify
    themselves with their permanent IDs as well as their names
    ("repo:owner@123/name@456:ref:..."), so a different repository can't take
    over the name later. Older repositories send "repo:owner/name:ref:...".
    The role accepts the exact value for this repository in either format.
    """
    subjects = [f"repo:{repo}:ref:refs/heads/{branch}"]
    if owner_id and repo_id:
        owner, name = repo.split("/")
        subjects.insert(0, f"repo:{owner}@{owner_id}/{name}@{repo_id}:ref:refs/heads/{branch}")
    return subjects


def _private_bucket(scope: Construct, id: str, **kwargs) -> s3.Bucket:
    """S3 bucket with every public-access block on, encryption at rest and TLS-only access."""
    return s3.Bucket(
        scope, id,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.S3_MANAGED,
        enforce_ssl=True,
        removal_policy=RemovalPolicy.RETAIN,
        **kwargs,
    )


class DashboardStack(Stack):
    def __init__(self, scope: Construct, id: str, *, github_repo: str, github_branch: str = "main",
                 github_owner_id: str | None = None, github_repo_id: str | None = None,
                 existing_oidc_provider_arn: str | None = None, alert_email: str | None = None,
                 monthly_budget_usd: int = 5, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # ---------- Logs: S3 server access logs and CloudFront standard logs ----------
        self.log_bucket = _private_bucket(
            self, "LogBucket",
            # CloudFront standard logging writes with ACLs, so this bucket must allow them
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
        )

        # ---------- Static site ----------
        self.site_bucket = _private_bucket(
            self, "SiteBucket",
            versioned=True,   # a bad deploy can be rolled back from earlier versions
            lifecycle_rules=[s3.LifecycleRule(noncurrent_version_expiration=Duration.days(30))],
            server_access_logs_bucket=self.log_bucket,
            server_access_logs_prefix="s3/site/",
        )

        self.distribution = cloudfront.Distribution(
            self, "Distribution",
            comment="Hip & knee replacement outcomes dashboard (synthetic data)",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
                compress=True,
            ),
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,   # North America and Europe edges only: cheapest
            enable_logging=True,
            log_bucket=self.log_bucket,
            log_file_prefix="cloudfront/",
        )

        # ---------- GitHub Actions deploy role (OIDC, no stored keys) ----------
        if existing_oidc_provider_arn:
            provider = iam.OidcProviderNative.from_oidc_provider_arn(
                self, "GitHubOidc", existing_oidc_provider_arn)
        else:
            provider = iam.OidcProviderNative(
                self, "GitHubOidc", url=GITHUB_OIDC_URL, client_ids=["sts.amazonaws.com"])

        self.deploy_role = iam.Role(
            self, "GitHubDeployRole",
            description=f"Deploys the dashboard from GitHub Actions ({github_repo}, {github_branch} branch only)",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                provider.oidc_provider_arn,
                conditions={
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                        "token.actions.githubusercontent.com:sub":
                            github_subjects(github_repo, github_branch, github_owner_id, github_repo_id),
                    },
                },
            ),
        )
        # Exactly what `aws s3 sync --delete` and a cache invalidation need, on this site only
        self.deploy_role.add_to_policy(iam.PolicyStatement(
            sid="ListSiteBucket", actions=["s3:ListBucket"], resources=[self.site_bucket.bucket_arn]))
        self.deploy_role.add_to_policy(iam.PolicyStatement(
            sid="WriteSiteFiles", actions=["s3:PutObject", "s3:DeleteObject"],
            resources=[self.site_bucket.arn_for_objects("*")]))
        self.deploy_role.add_to_policy(iam.PolicyStatement(
            sid="InvalidateCdnCache", actions=["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"],
            resources=[self.distribution.distribution_arn]))

        # ---------- Data check: upload a file, get a quality report ----------
        self.intake_bucket = _private_bucket(
            self, "IntakeBucket",
            server_access_logs_bucket=self.log_bucket,
            server_access_logs_prefix="s3/intake/",
            lifecycle_rules=[
                # Uploaded files are deleted after a week; reports kept for 90 days
                s3.LifecycleRule(prefix="incoming/", expiration=Duration.days(7)),
                s3.LifecycleRule(prefix="reports/", expiration=Duration.days(90)),
            ],
        )

        self.alerts = sns.Topic(
            self, "DataCheckAlerts",
            display_name="Dashboard data check",
            master_key=kms.Alias.from_alias_name(self, "SnsKey", "alias/aws/sns"),
            enforce_ssl=True,
        )
        if alert_email:
            self.alerts.add_subscription(subs.EmailSubscription(alert_email))
            self._add_budget(alert_email, monthly_budget_usd)

        log_group = logs.LogGroup(
            self, "DataCheckLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # A dedicated role instead of the AWS managed basic-execution policy,
        # so the function can only write to its own log group
        fn_role = iam.Role(self, "DataCheckRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
        log_group.grant_write(fn_role)

        self.data_check = lambda_.Function(
            self, "DataCheckFunction",
            description="Runs the dashboard's data-quality checks on files uploaded to incoming/",
            runtime=RUNTIME,
            architecture=ARCHITECTURE,   # Graviton: about 20% cheaper than x86 for the same work
            handler="handler.handler",
            code=data_check_code(),
            memory_size=1024,
            timeout=Duration.minutes(2),
            role=fn_role,
            log_group=log_group,
            environment={"TOPIC_ARN": self.alerts.topic_arn, "REPORT_PREFIX": "reports/"},
        )
        # Read uploads, write reports, publish results; nothing else
        fn_role.add_to_policy(iam.PolicyStatement(
            sid="ReadUploads", actions=["s3:GetObject"],
            resources=[self.intake_bucket.arn_for_objects("incoming/*")]))
        fn_role.add_to_policy(iam.PolicyStatement(
            sid="WriteReports", actions=["s3:PutObject"],
            resources=[self.intake_bucket.arn_for_objects("reports/*")]))
        self.alerts.grant_publish(self.data_check)

        # Only incoming/ triggers the function, so writing reports/ can't cause a loop
        self.intake_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(self.data_check),
            s3.NotificationKeyFilter(prefix="incoming/"),
        )

        # ---------- Outputs: the values GitHub needs ----------
        CfnOutput(self, "SiteUrl", value=f"https://{self.distribution.distribution_domain_name}")
        CfnOutput(self, "SiteBucketName", value=self.site_bucket.bucket_name,
                  description="GitHub variable AWS_SITE_BUCKET")
        CfnOutput(self, "DistributionId", value=self.distribution.distribution_id,
                  description="GitHub variable AWS_DISTRIBUTION_ID")
        CfnOutput(self, "DeployRoleArn", value=self.deploy_role.role_arn,
                  description="GitHub variable AWS_DEPLOY_ROLE_ARN")
        CfnOutput(self, "IntakeBucketName", value=self.intake_bucket.bucket_name,
                  description="Upload files to s3://<this bucket>/incoming/")

    def _add_budget(self, email: str, limit_usd: int) -> None:
        """Email when the account's monthly spend is forecast to pass the limit, or actually reaches 80% of it."""
        subscriber = budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL", address=email)
        budgets.CfnBudget(
            self, "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST", time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=limit_usd, unit="USD"),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type=kind, comparison_operator="GREATER_THAN",
                        threshold=pct, threshold_type="PERCENTAGE"),
                    subscribers=[subscriber])
                for kind, pct in (("ACTUAL", 80), ("FORECASTED", 100))
            ],
        )
