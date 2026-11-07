"""Publish the drift reference: the committed synthetic dataset, and nothing else.

The data-check Lambda compares every upload with s3://<ReferenceBucketName>/reference/discharges.csv.
It can only read that object, so it changes only when someone runs this script.

    python scripts/publish_reference.py --bucket <ReferenceBucketName> --dry-run
    python scripts/publish_reference.py --bucket <ReferenceBucketName>

It refuses to run unless data/synthetic_discharges.csv is tracked by git and has no
uncommitted changes, so what's published is always a file anyone can see in the
repository at a known commit. The object records that commit and the file's SHA-256;
the bucket is versioned, so earlier references stay available (the last 5, for a year).
"""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_FILE = "data/synthetic_discharges.csv"     # the only file this script will publish
REFERENCE_KEY = "reference/discharges.csv"           # must match REFERENCE_KEY in infra/dashboard_infra/stack.py


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def committed_reference() -> tuple[Path, str]:
    """The synthetic file and the commit it comes from; exits if it isn't exactly as committed."""
    if not git("ls-files", REFERENCE_FILE):
        sys.exit(f"{REFERENCE_FILE} isn't tracked by git; only the committed synthetic dataset can be the reference")
    if git("status", "--porcelain", "--", REFERENCE_FILE):
        sys.exit(f"{REFERENCE_FILE} has uncommitted changes; commit them (or restore the file) first")
    return REPO_ROOT / REFERENCE_FILE, git("rev-parse", "HEAD")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", required=True, help="the stack's ReferenceBucketName output")
    parser.add_argument("--profile", help="AWS CLI profile to use")
    parser.add_argument("--dry-run", action="store_true", help="show what would be published, then stop")
    args = parser.parse_args(argv)

    path, commit = committed_reference()
    body = path.read_bytes()
    sha256 = hashlib.sha256(body).hexdigest()
    print(f"Reference: {REFERENCE_FILE} at commit {commit[:12]} ({len(body):,} bytes, sha256 {sha256[:12]}…)")
    print(f"Target:    s3://{args.bucket}/{REFERENCE_KEY}")
    if args.dry_run:
        print("Dry run: nothing published.")
        return

    import boto3

    s3 = boto3.Session(profile_name=args.profile).client("s3")
    result = s3.put_object(Bucket=args.bucket, Key=REFERENCE_KEY, Body=body, ContentType="text/csv",
                           Metadata={"source": REFERENCE_FILE, "git-commit": commit, "sha256": sha256})
    print(f"Published version {result.get('VersionId')}. Drift reports name the version they compared with.")


if __name__ == "__main__":
    main()
