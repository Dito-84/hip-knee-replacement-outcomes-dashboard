"""CI roles for deploying the main stack from GitHub Actions: a stack of its own.

Three GitHub OIDC roles, each trusted only for this repository and only in one
context, so no AWS keys are stored anywhere:

* Diff: pull requests, and the plan job on main. Read-only, on these two stacks
  only (DescribeStacks, GetTemplate, GetTemplateSummary), for `cdk diff
  --method=template`. Not CDK's lookup role, which carries ReadOnlyAccess
  (including s3:GetObject on every bucket) while a pull request runs code from
  its own branch.
* Deploy: only jobs in the GitHub `production` environment, which waits for a
  required reviewer. It can assume CDK's bootstrap deploy and file-publishing
  roles, and inspect and execute change sets on the main stack (for the
  replacement guard). Nothing else directly.
* Smoke: the same environment. What scripts/smoke_upload.py needs: Cognito admin
  actions on the pool, reading the stack's outputs, and checking that website
  uploads were deleted.

Why a separate stack: if these roles lived in the stack they deploy, one bad
change could lock CI out of deploying the fix. This one is deployed rarely,
from a laptop (cdk deploy HipKneeDashboardCi).

Known trade-off (docs/well-architected.md): CDK's bootstrap CloudFormation
execution role has AdministratorAccess by default, so approving a deploy approves
admin-level changes, as a laptop deploy does. The approval gate, main-only trust
and the branch ruleset contain it; scoping that role is a separate phase.
"""

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
CDK_QUALIFIER = "hnb659fds"   # CDK's default bootstrap qualifier
MAIN_STACK = "HipKneeDashboard"
CI_STACK = "HipKneeDashboardCi"
ENVIRONMENT = "production"


def oidc_subjects(repo: str, contexts: list[str], owner_id: str | None, repo_id: str | None) -> list[str]:
    """The OIDC `sub` values for this repository in each context, in both of GitHub's formats.

    A context is what follows the repository in the subject: "pull_request",
    "ref:refs/heads/main" or "environment:production". Repositories created after
    July 15, 2026 also send their permanent IDs ("repo:owner@1/name@2:...").
    """
    subjects = [f"repo:{repo}:{c}" for c in contexts]
    if owner_id and repo_id:
        owner, name = repo.split("/")
        subjects = [f"repo:{owner}@{owner_id}/{name}@{repo_id}:{c}" for c in contexts] + subjects
    return subjects


class CiStack(Stack):
    def __init__(self, scope: Construct, id: str, *, github_repo: str, github_branch: str = "main",
                 github_owner_id: str | None = None, github_repo_id: str | None = None, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)
        account, region = self.account, self.region
        # The main stack created the account's GitHub OIDC provider; reference it by its fixed ARN
        provider = iam.OidcProviderNative.from_oidc_provider_arn(
            self, "GitHubOidc", f"arn:aws:iam::{account}:oidc-provider/{GITHUB_OIDC_HOST}")

        def github_role(role_id: str, description: str, contexts: list[str]) -> iam.Role:
            return iam.Role(
                self, role_id, description=description, max_session_duration=Duration.hours(1),
                assumed_by=iam.WebIdentityPrincipal(provider.oidc_provider_arn, conditions={"StringEquals": {
                    f"{GITHUB_OIDC_HOST}:aud": "sts.amazonaws.com",
                    f"{GITHUB_OIDC_HOST}:sub": oidc_subjects(github_repo, contexts, github_owner_id, github_repo_id),
                }}))

        stack_arns = [f"arn:aws:cloudformation:{region}:{account}:stack/{name}/*" for name in (MAIN_STACK, CI_STACK)]
        main_stack_arn = stack_arns[0]

        # ---------- Diff: read-only, these two stacks only ----------
        self.diff_role = github_role(
            "DiffRole", f"cdk diff from pull requests and the {github_branch} plan job ({github_repo}); read-only",
            ["pull_request", f"ref:refs/heads/{github_branch}"])
        self.diff_role.add_to_policy(iam.PolicyStatement(
            sid="ReadTheStacks",
            actions=["cloudformation:DescribeStacks", "cloudformation:GetTemplate",
                     "cloudformation:GetTemplateSummary"],
            resources=stack_arns))

        # ---------- Deploy: only behind the production environment's approval ----------
        self.deploy_role = github_role(
            "DeployRole", f"cdk deploy of {MAIN_STACK} from the GitHub '{ENVIRONMENT}' environment ({github_repo})",
            [f"environment:{ENVIRONMENT}"])
        bootstrap_roles = [f"arn:aws:iam::{account}:role/cdk-{CDK_QUALIFIER}-{kind}-role-{account}-{region}"
                           for kind in ("deploy", "file-publishing")]
        self.deploy_role.add_to_policy(iam.PolicyStatement(
            sid="UseCdkBootstrapRoles", actions=["sts:AssumeRole", "sts:TagSession"], resources=bootstrap_roles))
        # The replacement guard reads the change set before executing it
        self.deploy_role.add_to_policy(iam.PolicyStatement(
            sid="InspectAndExecuteChangeSets",
            actions=["cloudformation:DescribeChangeSet", "cloudformation:ExecuteChangeSet",
                     "cloudformation:DeleteChangeSet", "cloudformation:DescribeStacks",
                     "cloudformation:DescribeStackEvents"],
            resources=[main_stack_arn]))

        # ---------- Smoke: what scripts/smoke_upload.py needs, nothing more ----------
        self.smoke_role = github_role(
            "SmokeRole", f"End-to-end smoke test after a deploy, from the '{ENVIRONMENT}' environment ({github_repo})",
            [f"environment:{ENVIRONMENT}"])
        self.smoke_role.add_to_policy(iam.PolicyStatement(
            sid="ReadStackOutputs", actions=["cloudformation:DescribeStacks"], resources=[main_stack_arn]))
        self.smoke_role.add_to_policy(iam.PolicyStatement(
            sid="ThrowawayAccounts",
            actions=["cognito-idp:AdminCreateUser", "cognito-idp:AdminSetUserPassword",
                     "cognito-idp:AdminInitiateAuth", "cognito-idp:AdminDeleteUser"],
            resources=[f"arn:aws:cognito-idp:{region}:{account}:userpool/*"]))
        intake = "arn:aws:s3:::hipkneedashboard-intakebucket*"
        # Checking a website upload was deleted: without ListBucket, S3 answers 403 instead of 404
        self.smoke_role.add_to_policy(iam.PolicyStatement(
            sid="SeeWebUploadsAreGone", actions=["s3:ListBucket"], resources=[intake],
            conditions={"StringLike": {"s3:prefix": ["incoming/web/*"]}}))
        self.smoke_role.add_to_policy(iam.PolicyStatement(
            sid="HeadWebUploads", actions=["s3:GetObject"], resources=[f"{intake}/incoming/web/*"]))

        for name, role, var in (("Diff", self.diff_role, "AWS_DIFF_ROLE_ARN"),
                                ("StackDeploy", self.deploy_role, "AWS_STACK_DEPLOY_ROLE_ARN"),
                                ("Smoke", self.smoke_role, "AWS_SMOKE_ROLE_ARN")):
            CfnOutput(self, f"{name}RoleArn", value=role.role_arn, description=f"GitHub variable {var}")
