# Well-Architected review

A review of the AWS side of this project against the six pillars of the [AWS Well-Architected Framework](https://docs.aws.amazon.com/wellarchitected/latest/framework/welcome.html). For each pillar: what's in place, the trade-offs I chose, and what I'd do next. The design details are in [`infra/README.md`](../infra/README.md); the reasoning behind the bigger decisions is in the [case study](case-study.md).

**Scope.** A public portfolio project on **synthetic data only**. Three parts:

- **Static dashboard:** S3 and CloudFront, deployed from GitHub Actions through OIDC.
- **Data check:** an S3-triggered Lambda that rejects anything not marked synthetic, then runs quality and drift checks and reports counts only.
- **Authenticated uploads:** invited reviewers sign in with Cognito and upload sample batches through an API Gateway HTTP API and presigned S3 POSTs.

There's one Region (`us-east-1`), one account, and a $5 monthly budget alert.

## Operational excellence

**In place**
- **Everything is code:** both stacks are AWS CDK in Python. No resource is created by hand. The GitHub environment, variables and ruleset are the only manual settings, and the README lists each one.
- **Every change goes through a pull request** with two required checks:
  - `test`: no-real-data check, lint, tests with a 90% coverage gate, build, CDN integrity.
  - `infra`: template and Lambda tests, a real build of the Lambda package, and `cdk synth` with cdk-nag, which fails on any unexplained security finding.
- **Infrastructure pull requests show their effect before merging.** A read-only workflow posts the `cdk diff` of both stacks, with the account ID and email addresses removed.
- **An end-to-end smoke test** ([`smoke_upload.py`](../scripts/smoke_upload.py)) proves the deployed upload path works, including each refusal: no token, an expiry change, someone else's result, an oversized file, an unmarked file, an expired account. CI can't reach AWS, so this is the only proof that the deny paths work for real.
- **Runbooks** in the README and the project notes cover deploying, inviting and removing reviewers, resending invitations, and running a demo.

**Learning from failures.** The first deploy of authenticated uploads hit four problems. None of them was visible to synth, cdk-nag or the template tests. Each now has a test that fails without its fix (PRs #32 to #35):

| What happened | Cause | Fix |
|---|---|---|
| The deploy failed and rolled back | CloudFormation created the API stage before the route its throttle setting names, and API Gateway refuses settings for a route that doesn't exist yet | The stage depends on the route |
| The smoke test found that a user could change their own expiry date | An app client's **empty** `WriteAttributes` list means "unset", which makes **every** attribute writable | An explicit write list (`email` only), and the expiry moved to an **immutable** attribute. No reviewer account existed yet, so nothing was exposed |
| The crash alarm emailed when an expired account was refused | Cognito can refuse a sign-in only through an exception from the trigger, and Lambda counts that as an error | The alarm counts logged errors other than `AccountExpired`, through a metric filter |
| Every browser sign-in failed with `invalid_grant` | The `email` scope needs the client to read `email` **and** `email_verified` | The client reads `email_verified`. Found by ruling out PKCE, the request, the client's OAuth settings and the trigger, with CloudTrail and the token endpoint documentation |

The pattern was the same each time: the failure surfaced from a live check, and it got a regression test before it was closed.

- **The main stack deploys from CI, after approval.** A merge shows its template diff, waits for a required reviewer in the `production` environment, then creates a change set. A **replacement guard** refuses the change set if a stateful resource would be replaced or removed. Then the change set is executed and the smoke test runs. Deploying from a laptop is the break-glass route only.

**Next.** A CloudWatch dashboard and an hourly synthetic canary for the happy path. Today the alarms only report what's broken; they don't show what's working.

## Security

**In place**
- **Identity and access**
  - **No long-lived AWS keys exist.** GitHub signs in through OIDC, and each role's trust policy names the exact repository and context: `main`, pull requests, or the `production` environment.
  - **Every IAM role has its own policy, written out by hand.** No AWS managed policies, and wildcards appear only where an ARN ends in a generated ID, each documented in [`nag.py`](../infra/dashboard_infra/nag.py).
  - **Reviewers never become AWS principals.** Cognito issues them JWTs that one API accepts. Accounts are created by me only, expire on an immutable date, and get 30-minute tokens. The page discards the refresh token, because Cognito doesn't run the expiry check on refresh.
- **Data protection**
  - **Real patient data is never uploaded, and that rule is enforced.** A synthetic-only guard runs first on every file and deletes anything without the marker. The upload page runs the same rules in the browser, so a wrong file never leaves the laptop. The README says plainly that this catches accidents, not intent.
  - **Encryption and transport:** every bucket blocks public access, encrypts at rest and refuses plain HTTP. The alert topic uses a customer managed KMS key.
  - **Nothing from a file is shown, emailed or logged:** only counts, column names and check names. Unexpected errors are reported by their type only, because an exception message can quote a cell. Tests check this for refused files, drifted files and crashes.
  - **Deletion means deletion.** The intake bucket is deliberately unversioned, because in a versioned bucket a delete only adds a marker and the refused file would still be stored.
- **Infrastructure protection**
  - **The upload page's Content-Security-Policy** allows only its own script and style, by hash, and connections only to the API, the Cognito domain and the bucket.
  - **The uploads path:** a bucket policy lets only the presign role write under `incoming/web/`. The API is throttled, and daily limits in DynamoDB cap uploads per user and overall.
- **Detection:** cdk-nag on every synth, GitHub secret scanning and push protection, Dependabot, and a branch ruleset that applies to admins too.

**Known trade-off: CDK's bootstrap execution role has `AdministratorAccess`**

CloudFormation deploys both stacks through the execution role that `cdk bootstrap` created, and by default that role has `AdministratorAccess`. So **approving a deploy is approving admin-level changes**, whether the deploy runs from CI or from my laptop. The Deploy role itself can only assume CDK's roles and inspect change sets, but anything a template asks for, CloudFormation can do.

*How it's contained today:*
- Only jobs in the GitHub `production` environment can assume the Deploy role, and that environment waits for my approval and accepts only `main`.
- `main` takes changes only through pull requests with both checks passing, under a ruleset that applies to admins too.
- Every deploy shows its diff before approval, and the replacement guard stops any change that would replace or remove a stateful resource.

*Why it isn't fixed in this change:*
- Re-bootstrapping replaces the execution role that every deploy of both stacks depends on. A policy that's even slightly too narrow fails a deploy partway through and rolls it back.
- Scoping is also only meaningful with a **permissions boundary**. These stacks create IAM roles, so an execution role that can create unbounded roles can grant itself anything, however narrow its own policy looks.
- That deserves its own tested change, not a side effect of adding CI.

*The plan, as a separate phase:*
1. **Write the policy.** A customer managed execution policy covering only the services these stacks use: S3, CloudFront, Lambda, API Gateway, Cognito, DynamoDB, SNS, SQS, KMS, CloudWatch and Logs, Budgets, and IAM. Its IAM actions apply only to roles created with a project permissions boundary (the `iam:PermissionsBoundary` condition key). That boundary caps any role the stacks create, so nothing they create can exceed it.
2. **Bootstrap a second environment alongside the current one.** Use a new qualifier (`cdk bootstrap --qualifier scoped --cloudformation-execution-policies <policy ARN>`), so the existing one is untouched.
3. **Prove it.** Deploy a copy of both stacks under the new qualifier, including a change that creates a role, and fix the policy until a full deploy and an update succeed. The smoke test must pass against the copy.
4. **Switch.** Point the real stacks at the new qualifier, update the Deploy role to assume the new bootstrap roles, then remove the old bootstrap.

If that phase never happens, the risk stays where it is today: bounded by who can approve a deploy, which is one person. That makes MFA on that person's GitHub and AWS accounts a requirement, not an option.

**Other decisions**
- **No AWS WAF.** It can't be attached to an HTTP API, so it couldn't protect the upload API. On CloudFront it would protect only static files. It would also cost more than the whole budget. CloudFront includes AWS Shield Standard.
- **Cognito's Plus tier (threat protection) isn't used:** no free tier, and sign-up is closed.
- **MFA is optional** for reviewers, using authenticator apps. Their accounts are short-lived and can reach nothing but the upload API.

**Next.** The execution-role phase above. A CloudTrail trail, since the first trail's management events are free, and IAM Access Analyzer for external access, which is also free. A custom domain with an ACM certificate, which would allow a TLS 1.2 minimum on CloudFront and remove that cdk-nag suppression.

## Reliability

**In place**
- **Only managed, multi-AZ, serverless services.** There are no servers or containers to patch or scale.
- **The data check fails loudly.** S3 invokes it asynchronously, so a crash would otherwise look like a quiet week. Now: two retries, a one-hour maximum event age, then an encrypted dead-letter queue kept 14 days. Alarms fire on errors and on anything reaching the queue.
- **A website upload is always deleted.** The check deletes it in a `finally` block, so a crash doesn't skip that. A retry then finds nothing and keeps the error report. A 1-day lifecycle rule is the backstop for a timeout.
- **State can't be lost by accident:**
  - buckets are retained if a stack is deleted;
  - the site and the drift reference are versioned;
  - the user pool has deletion protection;
  - both stacks have termination protection;
  - the CI deploy's guard refuses to replace or remove a stateful resource.
- **Failed deploys roll back on their own.** That happened once, on the stage-ordering problem above, and left the live stack unchanged.

**Recovery objectives.**

| What fails | Recovery time | Data lost |
|---|---|---|
| Bad site deploy | Minutes: revert the commit, or restore a version from the bucket | None |
| A stack lost | About 15 minutes: redeploy from git | Nothing of value: the data is regenerated from a seed, reports are counts only and short-lived, and reviewer accounts are recreated by script |
| A Region outage | Not designed for: the site and uploads are down until it recovers | None |

Multi-Region would cost more than it protects for a portfolio site.

**Next.** A synthetic canary for the upload journey, so a problem shows up before a reviewer finds it.

## Performance efficiency

**In place**
- **CloudFront** serves the site from edge locations, with compression and HTTP/3. The page is one self-contained HTML file plus one integrity-pinned library.
- **Uploads go straight from the browser to S3** through a presigned POST, so the API never carries file data. That avoids API Gateway's payload limit and extra latency, and the API functions stay small: 256 MB, 10-second timeout.
- **The data check runs on Graviton (ARM64)** at 1 GB. The package is a zip, kept to 183 MB by fixing a library that loaded plotting code it didn't need, rather than moving to a container image.
- **The upload page polls every 3 seconds, for up to 2 minutes.** That's simpler than WebSockets, and enough for a check that takes seconds.

**Next.** Pick the data-check function's memory with Lambda Power Tuning rather than the 1 GB estimate: a measured cost-against-speed curve.

## Cost optimization

**In place**
- **Pay per use everywhere:** Lambda, an HTTP API (about a third of a REST API's price), on-demand DynamoDB, and Cognito Essentials, free under 10,000 monthly users.
- **The one fixed cost** is the alert topic's KMS key, about $1 a month.
- **Lifecycle rules:**
  - uploads after 7 days (website uploads after 1);
  - reports and logs after 90 (website results after 7);
  - old site versions after 30.
- **Log retention:** one month.
- **CloudFront** uses its cheapest price class. ARM64 Lambda costs about 20% less than x86.
- **A $5 budget** emails at 80% of actual spend and at 100% of forecast spend.
- **Decisions made on cost:** no WAF (more than the whole budget), no Cognito Plus tier, no NAT gateway or VPC (nothing needs one).

**Estimate.** About $1 a month at demo volume, nearly all of it the KMS key. The site, the data check, uploads, alarms and the CI pipeline fit within free tiers or cost fractions of a cent. GitHub Actions minutes are free for public repositories.

## Sustainability

**In place**
- **Serverless and scale-to-zero:** nothing runs between requests, and no capacity sits idle.
- **Graviton (ARM64)** for the heaviest function, which does more work per watt.
- **Data that isn't needed doesn't stay.** Files are deleted once checked, and lifecycle rules remove reports, logs and old versions. The drift reference keeps only five old versions.
- **A small footprint.** A single-page site served from the edge, a slim Lambda package, and no container images or always-on databases.

**Next.** Measure before resizing. Power Tuning's results would show whether 1 GB is more memory than the check needs.
