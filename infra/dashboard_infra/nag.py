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
            reason="Static files only, no forms, logins or APIs to protect. AWS WAF would cost more than the "
                   "rest of the stack combined; CloudFront's built-in AWS Shield Standard covers network-level "
                   "attacks."),
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
            reason="File names aren't known in advance, so access is scoped by prefix: read-only under "
                   "incoming/, write-only under reports/."),
    ], apply_to_children=True)

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
