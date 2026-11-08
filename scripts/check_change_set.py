"""Replacement guard: refuse a change set that would replace or remove a stateful resource.

Run by the deploy workflow after `cdk deploy --method=prepare-change-set` and before
the change set is executed:

    python scripts/check_change_set.py --stack HipKneeDashboard --change-set ci-abc1234

The template diff shown before approval can't say whether CloudFormation will
replace a resource (that needs a change set, which needs the deploy role). This
reads the change set itself and fails if a stateful resource would be replaced,
or might be ("Conditional"), or removed. Exit codes: 0 safe to execute, 1 refused
or the change set failed for a real reason, 3 nothing to deploy (no change set, or
CloudFormation found no changes: it marks such a change set FAILED rather than
deleting it, and it can't be executed). With --allow-replacement it reports but
doesn't refuse: for a deliberate replacement, run the workflow by hand with that
input set.
"""

import argparse
import json
import sys

# Replacing or removing any of these loses data, users, or a public address
STATEFUL = {
    "AWS::S3::Bucket": "bucket (files, reports, the drift reference, the site)",
    "AWS::Cognito::UserPool": "user pool (every reviewer account)",
    "AWS::Cognito::UserPoolDomain": "sign-in domain (the page's sign-in address)",
    "AWS::DynamoDB::Table": "table (upload limits)",
    "AWS::KMS::Key": "KMS key (the alert topic can't be read without it)",
    "AWS::CloudFront::Distribution": "CloudFront distribution (the site's address)",
    "AWS::SQS::Queue": "queue (failed checks waiting to be looked at)",
}
NO_CHANGES = 3
EMPTY_REASONS = ("didn't contain changes", "No updates are to be performed")


def nothing_to_change(status: str, reason: str) -> bool:
    """A change set CloudFormation refused because the stack is already up to date."""
    return status == "FAILED" and any(r in (reason or "") for r in EMPTY_REASONS)


def violations(changes: list[dict]) -> list[str]:
    """Plain-language problems in a change set's Changes list (DescribeChangeSet format)."""
    found = []
    for change in changes:
        rc = change.get("ResourceChange", {})
        kind = rc.get("ResourceType", "")
        if kind not in STATEFUL:
            continue
        name = rc.get("LogicalResourceId", "?")
        what = STATEFUL[kind]
        if rc.get("Action") == "Remove":
            found.append(f"{name}: would REMOVE the {what}")
        elif rc.get("Replacement") == "True":
            found.append(f"{name}: would REPLACE the {what}")
        elif rc.get("Replacement") == "Conditional":
            found.append(f"{name}: might replace the {what} (CloudFormation can't tell until it runs)")
    return found


def summary(changes: list[dict]) -> str:
    rows = ["| Action | Resource | Type | Replacement |", "|---|---|---|---|"]
    for change in changes:
        rc = change.get("ResourceChange", {})
        rows.append(f"| {rc.get('Action', '')} | {rc.get('LogicalResourceId', '')} | {rc.get('ResourceType', '')} "
                    f"| {rc.get('Replacement', '') or '-'} |")
    return "\n".join(rows)


def fetch_changes(stack: str, change_set: str) -> tuple[str, str, list[dict]] | None:
    """(status, status reason, changes), or None if there's no such change set."""
    import boto3
    from botocore.exceptions import ClientError

    cfn = boto3.client("cloudformation")
    changes, token, status, reason = [], None, "", ""
    try:
        while True:
            kwargs = {"StackName": stack, "ChangeSetName": change_set}
            if token:
                kwargs["NextToken"] = token
            page = cfn.describe_change_set(**kwargs)
            status, reason = page.get("Status", ""), page.get("StatusReason", "")
            changes += page.get("Changes", [])
            token = page.get("NextToken")
            if not token:
                return status, reason, changes
    except ClientError as err:
        if err.response["Error"].get("Code") == "ChangeSetNotFound":
            return None
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stack", required=True)
    parser.add_argument("--change-set", required=True)
    parser.add_argument("--allow-replacement", action="store_true")
    parser.add_argument("--from-file", help="read the Changes list from a JSON file instead of AWS (testing)")
    args = parser.parse_args(argv)

    if args.from_file:
        with open(args.from_file) as f:
            data = json.load(f)
        fetched = (data.get("Status", "CREATE_COMPLETE"), data.get("StatusReason", ""), data.get("Changes", [])) \
            if isinstance(data, dict) else ("CREATE_COMPLETE", "", data)
    else:
        fetched = fetch_changes(args.stack, args.change_set)
    if fetched is None:
        print("No change set: CloudFormation found nothing to change.")
        return NO_CHANGES
    status, reason, changes = fetched
    if nothing_to_change(status, reason):
        print("CloudFormation found nothing to change (the stack is already up to date).")
        return NO_CHANGES
    if status != "CREATE_COMPLETE":
        print(f"::error title=Change set not ready::status {status}: {reason}")
        return 1
    print(summary(changes))
    found = violations(changes)
    for problem in found:
        print(f"::error title=Replacement guard::{problem}")
    if found and not args.allow_replacement:
        print(f"\nRefused: {len(found)} stateful resource(s) would be replaced or removed. If that's deliberate, "
              "run the 'Deploy stack' workflow by hand with 'allow replacement' ticked.")
        return 1
    if found:
        print("\nAllowed by --allow-replacement.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
