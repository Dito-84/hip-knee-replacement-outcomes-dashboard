"""Documented exceptions to the cdk-nag AWS Solutions checks.

Every other AWS Solutions rule must pass: app.py and the tests fail on any
finding that isn't listed here with a reason.
"""

from aws_cdk import Stack
from cdk_nag import NagPackSuppression, NagSuppressions


def add_suppressions(stack: Stack) -> None:
    NagSuppressions.add_resource_suppressions(stack.distribution, [
        NagPackSuppression(
            id="AwsSolutions-CFR4",
            reason="The site uses the default *.cloudfront.net certificate, whose TLS policy CloudFront sets. "
                   "A minimum of TLS 1.2 can be enforced once a custom domain with an ACM certificate is added."),
        NagPackSuppression(
            id="AwsSolutions-CFR1",
            reason="A public portfolio site of synthetic data: there is no reason to block any country."),
        NagPackSuppression(
            id="AwsSolutions-CFR2",
            reason="CloudFront serves static files only. The upload page's sign-in and API are separate services "
                   "(Cognito, an HTTP API, which WAF can't attach to), protected by invitation-only accounts, "
                   "throttling and daily limits. AWS WAF would cost more than the rest of the stack combined; "
                   "CloudFront's built-in AWS Shield Standard covers network-level attacks."),
    ])

    NagSuppressions.add_resource_suppressions(stack.deploy_role, [
        NagPackSuppression(
            id="AwsSolutions-IAM5",
            applies_to=[f"Resource::<{stack.get_logical_id(stack.site_bucket.node.default_child)}.Arn>/*"],
            reason="Deploys replace any file on the site, so writes cover every object in this one bucket. "
                   "Actions are limited to PutObject and DeleteObject."),
    ], apply_to_children=True)

    data_check_role = stack.data_check.role
    intake = stack.get_logical_id(stack.intake_bucket.node.default_child)
    NagSuppressions.add_resource_suppressions(data_check_role, [
        NagPackSuppression(
            id="AwsSolutions-IAM5",
            applies_to=[f"Resource::<{intake}.Arn>/incoming/*", f"Resource::<{intake}.Arn>/reports/*"],
            reason="File names aren't known in advance, so access is scoped by prefix: read and delete "
                   "under incoming/ (rejected files and checked website uploads are deleted), write-only "
                   "under reports/."),
    ], apply_to_children=True)

    uploads = stack.uploads
    NagSuppressions.add_resource_suppressions(uploads.user_pool, [
        NagPackSuppression(
            id="AwsSolutions-COG2",
            reason="MFA is optional (authenticator apps only) by decision: reviewers are invited for a few days "
                   "to upload synthetic files. Their accounts are admin-created, time-limited and can reach "
                   "nothing but the upload API."),
        NagPackSuppression(
            id="AwsSolutions-COG8",
            reason="The Plus tier (threat protection) has no free tier and costs more than the rest of the stack. "
                   "Sign-up is closed, accounts expire, and the API is throttled with daily upload limits. "
                   "Essentials, free under 10,000 users a month, already gives managed login."),
    ])
    NagSuppressions.add_resource_suppressions(uploads.presign.role, [
        NagPackSuppression(
            id="AwsSolutions-IAM5",
            applies_to=[f"Resource::<{intake}.Arn>/incoming/web/*"],
            reason="Each upload gets a new key (incoming/web/<user>/<uuid>.csv), so signing is scoped by prefix. "
                   "The presigned POST policy fixes the exact key, and the bucket policy lets only this role "
                   "write there."),
    ], apply_to_children=True)
    NagSuppressions.add_resource_suppressions(uploads.results.role, [
        NagPackSuppression(
            id="AwsSolutions-IAM5",
            applies_to=[f"Resource::<{intake}.Arn>/reports/web/*"],
            reason="Results are per upload (reports/web/<user>/<uuid>.csv.json), so reading is scoped by prefix. "
                   "Read-only, and the function always builds the key from the caller's verified sub."),
    ], apply_to_children=True)

    NagSuppressions.add_resource_suppressions(stack.failed_checks, [
        NagPackSuppression(
            id="AwsSolutions-SQS3",
            reason="This queue is the dead-letter queue: the data-check function's on-failure destination. "
                   "Nothing reads from it automatically, so it needs no dead-letter queue of its own."),
    ])

    # CDK's own helper that attaches the S3 -> Lambda notification at deploy time
    for child in stack.node.children:
        if child.node.id.startswith("BucketNotificationsHandler"):
            NagSuppressions.add_resource_suppressions(child, [
                NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    applies_to=["Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/"
                                "AWSLambdaBasicExecutionRole"],
                    reason="CDK-managed custom resource that configures the bucket notification; its role is "
                           "created by CDK and only writes its own logs."),
            ], apply_to_children=True)


def add_ci_suppressions(stack: Stack) -> None:
    """The CI stack's documented exceptions. Its roles can't name the main stack's resources exactly:
    a cross-stack reference would lock those resources to this stack's exports."""
    NagSuppressions.add_resource_suppressions(stack.deploy_role, [
        NagPackSuppression(
            id="AwsSolutions-IAM5",
            applies_to=[f"Resource::arn:aws:cloudformation:{stack.region}:<AWS::AccountId>:stack/HipKneeDashboard/*"],
            reason="A stack ARN ends in a generated ID; the name is fixed. Change-set actions on this one stack only."),
    ], apply_to_children=True)
    NagSuppressions.add_resource_suppressions(stack.diff_role, [
        NagPackSuppression(
            id="AwsSolutions-IAM5",
            applies_to=[f"Resource::arn:aws:cloudformation:{stack.region}:<AWS::AccountId>:stack/{name}/*"
                        for name in ("HipKneeDashboard", "HipKneeDashboardCi")],
            reason="A stack ARN ends in a generated ID; the names are fixed. Read-only actions on these two stacks."),
    ], apply_to_children=True)
    NagSuppressions.add_resource_suppressions(stack.smoke_role, [
        NagPackSuppression(
            id="AwsSolutions-IAM5",
            applies_to=[
                f"Resource::arn:aws:cloudformation:{stack.region}:<AWS::AccountId>:stack/HipKneeDashboard/*",
                f"Resource::arn:aws:cognito-idp:{stack.region}:<AWS::AccountId>:userpool/*",
                "Resource::arn:aws:s3:::hipkneedashboard-intakebucket*",
                "Resource::arn:aws:s3:::hipkneedashboard-intakebucket*/incoming/web/*",
            ],
            reason="The pool ID and bucket name are generated by the main stack; naming them here would need "
                   "cross-stack exports that lock them. The account has one user pool. Actions are limited to "
                   "throwaway-account admin calls, reading this stack, and checking website uploads under "
                   "incoming/web/ were deleted (ListBucket restricted to that prefix)."),
    ], apply_to_children=True)
