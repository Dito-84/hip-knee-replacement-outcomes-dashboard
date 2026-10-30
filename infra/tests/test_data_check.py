"""The data-check Lambda's logic, run locally against the synthetic data.

S3 and SNS are replaced with in-memory fakes, so no AWS account is needed.
"""

import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = REPO_ROOT / "data" / "synthetic_discharges.csv"
sys.path.insert(0, str(REPO_ROOT))                                  # outcomes/
sys.path.insert(0, str(REPO_ROOT / "infra" / "lambda" / "data_check"))  # handler.py


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:alerts")
    import handler as module
    module = importlib.reload(module)
    module.s3, module.sns = FakeS3(), FakeSns()
    return module


class FakeS3:
    def __init__(self):
        self.objects = {}

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[(bucket, key)])

    def put_object(self, Bucket, Key, Body, **_):
        self.objects[(Bucket, Key)] = Body


class FakeSns:
    def __init__(self):
        self.messages = []

    def publish(self, **kwargs):
        self.messages.append(kwargs)


def s3_event(bucket, key):
    return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}


def broken_copy(tmp_path):
    """The synthetic file with one discharge moved before its admission."""
    raw = pd.read_csv(SYNTHETIC, dtype=object, keep_default_na=False)
    date_cols = [c for c in raw.columns if "Discharge Date" in c]
    adm_cols = [c for c in raw.columns if "Admission Date" in c]
    raw.loc[0, date_cols[0]] = "01/01/1990" if "/" in raw.loc[0, adm_cols[0]] else "1990-01-01"
    path = tmp_path / "broken.csv"
    raw.to_csv(path, index=False)
    return path


def test_synthetic_file_passes(handler):
    report = handler.check_file(SYNTHETIC)
    assert report["status"] == "passed"
    assert report["errors"] == 0
    assert report["records"] > 3000


def test_bad_dates_fail_the_check(handler, tmp_path):
    report = handler.check_file(broken_copy(tmp_path))
    assert report["status"] == "failed"
    assert any(f["check"] == "discharge before admission" for f in report["findings"])


def test_unsupported_and_unreadable_files(handler, tmp_path):
    (tmp_path / "notes.txt").write_text("hello")
    assert handler.check_file(tmp_path / "notes.txt")["status"] == "skipped"
    (tmp_path / "wrong.csv").write_text("a,b\n1,2\n")
    report = handler.check_file(tmp_path / "wrong.csv")
    assert report["status"] == "unreadable"


def test_handler_writes_report_and_notifies(handler):
    handler.s3.objects[("intake", "incoming/My File.csv")] = SYNTHETIC.read_bytes()
    result = handler.handler(s3_event("intake", "incoming/My+File.csv"), None)   # S3 URL-encodes keys

    assert result == {"checked": [{"key": "incoming/My File.csv", "status": "passed"}]}
    report = json.loads(handler.s3.objects[("intake", "reports/My File.csv.json")])
    assert report["status"] == "passed" and report["source_key"] == "incoming/My File.csv"
    [message] = handler.sns.messages
    assert message["Subject"] == "Data check PASSED: My File.csv"
    assert "s3://intake/reports/My File.csv.json" in message["Message"]


def test_report_and_notification_contain_no_record_values(handler, tmp_path):
    """Only counts and check names leave the function, never values from the file."""
    path = broken_copy(tmp_path)
    handler.s3.objects[("intake", "incoming/broken.csv")] = path.read_bytes()
    handler.handler(s3_event("intake", "incoming/broken.csv"), None)

    raw = pd.read_csv(path, dtype=object, keep_default_na=False)
    outgoing = handler.s3.objects[("intake", "reports/broken.csv.json")].decode()
    outgoing += json.dumps(handler.sns.messages)
    values = {v for col in raw.columns for v in raw[col].unique() if len(str(v)) >= 6}
    leaked = [v for v in values if v in outgoing]
    assert leaked == []


def test_subject_fits_sns_limit(handler):
    subject, _ = handler.summary({"file": "x" * 200, "status": "skipped", "reason": "r"})
    assert len(subject) <= 100


@pytest.mark.skipif("not config.getoption('--bundle')", reason="downloads pandas; run with --bundle")
def test_bundled_package_runs_on_its_own(tmp_path):
    """Build the Lambda package (for this machine's platform) and run it in a clean interpreter."""
    from dashboard_infra.bundling import assemble

    out = tmp_path / "package"
    tag = f"{sys.version_info.major}.{sys.version_info.minor}"
    assemble(out, python_version=tag, platforms=["manylinux_2_28_x86_64", "manylinux_2_17_x86_64",
                                                 "manylinux2014_x86_64"])
    assert (out / "handler.py").exists() and (out / "outcomes" / "quality.py").exists()
    assert not (out / "pandas" / "tests").exists()
    size_mb = sum(p.stat().st_size for p in out.rglob("*") if p.is_file()) / 1e6
    assert size_mb < 200, f"package is {size_mb:.0f} MB; Lambda's limit is 250 MB unzipped"

    data = tmp_path / "upload.csv"
    shutil.copy(SYNTHETIC, data)
    # -I ignores the environment and the current directory: only the package and the stdlib/boto3
    code = (f"import sys, json, pathlib; sys.path.insert(0, {str(out)!r}); import handler, pandas; "
            f"assert pandas.__file__.startswith({str(out)!r}); "
            f"print(json.dumps(handler.check_file(pathlib.Path({str(data)!r}))))")
    run = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True, text=True,
                         env={"AWS_DEFAULT_REGION": "us-east-1"})
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout.strip().splitlines()[-1])["status"] == "passed"
