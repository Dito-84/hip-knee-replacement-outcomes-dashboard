"""The data-check Lambda's logic, run locally against the synthetic data.

S3 and SNS are replaced with in-memory fakes, so no AWS account is needed.
The drift reference in the fake bucket is the synthetic CSV, and every
drifted batch is made from it: no real data is used anywhere.
"""

import importlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = REPO_ROOT / "data" / "synthetic_discharges.csv"
sys.path.insert(0, str(REPO_ROOT))                                  # outcomes/
sys.path.insert(0, str(REPO_ROOT / "infra" / "lambda" / "data_check"))  # handler.py


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:alerts")
    monkeypatch.setenv("REFERENCE_BUCKET", REFERENCE[0])
    monkeypatch.setenv("REFERENCE_KEY", REFERENCE[1])
    import handler as module
    module = importlib.reload(module)
    module.s3, module.sns = FakeS3(), FakeSns()
    module.s3.objects[REFERENCE] = SYNTHETIC.read_bytes()
    return module


REFERENCE = ("reference-bucket", "reference/discharges.csv")


class FakeS3:
    """In-memory S3 that, like the real role, can't write to the reference bucket."""

    def __init__(self):
        self.objects = {}
        self.writes = []

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[(bucket, key)])

    def get_object(self, Bucket, Key, **_):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)]), "VersionId": "v1"}

    def put_object(self, Bucket, Key, Body, **_):
        self.writes.append((Bucket, Key))
        if Bucket == REFERENCE[0]:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "PutObject")
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


def drifted_copy(tmp_path):
    """The synthetic file with four kinds of drift, each one a known answer.

    - counts sent as Yes/No instead of 0/1/2 (a coding change: number → text; load() would read them as 0)
    - "Hospital A" relabelled "Hosp. A" (16% of stays arrive under a name the reference never had)
    - a new Measure code on 10% of rows
    - "Excluded (Yes/No)" left blank on 20% of rows (it is never blank in the reference)
    """
    raw = pd.read_csv(SYNTHETIC, dtype=object, keep_default_na=False)
    tka = [c for c in raw.columns if c.startswith("Number of TKAs")][0]
    raw[tka] = raw[tka].map({"0": "No", "1": "Yes", "2": "Yes"})
    raw["Hospital"] = raw["Hospital"].replace("Hospital A", "Hosp. A")
    every_tenth = raw.index % 10 == 0
    raw.loc[every_tenth, "Measure"] = "THA/TKA Readmission"
    raw.loc[raw.index % 5 == 1, "Excluded (Yes/No)"] = ""
    path = tmp_path / "drifted.csv"
    raw.to_csv(path, index=False)
    return path


def stable_copy(tmp_path, seed, n_stays):
    """A fresh synthetic batch from another seed: same process, so no real drift."""
    from outcomes.synthetic import generate

    path = tmp_path / f"stable_{seed}_{n_stays}.csv"
    generate(n_stays=n_stays, seed=seed).to_csv(path, index=False)
    return path


def test_drifted_batch_alerts_with_counts_only(handler, tmp_path):
    path = drifted_copy(tmp_path)
    handler.s3.objects[("intake", "incoming/drifted.csv")] = path.read_bytes()
    handler.handler(s3_event("intake", "incoming/drifted.csv"), None)

    report = json.loads(handler.s3.objects[("intake", "reports/drifted.csv.json")])
    drift = report["drift"]
    assert drift["status"] == "alert"
    assert drift["reference"] == {"key": "reference/discharges.csv", "version_id": "v1"}
    reasons = {a["column"]: " ".join(a["reasons"]) for a in drift["alerts"]}
    assert set(reasons) == {"n_tka", "hospital", "measure", "excluded"}
    assert "type changed (number → category)" in reasons["n_tka"]
    assert "1 new value(s)" in reasons["hospital"]
    assert "1 new value(s)" in reasons["measure"]
    assert "blanks 0.0% → 20.0%" in reasons["excluded"]

    [message] = handler.sns.messages
    assert message["Subject"].startswith("Data check") and "DRIFT" in message["Subject"]
    assert "[DRIFT] hospital:" in message["Message"]

    # Nothing from the records leaves the function: not the new labels, not any other value
    outgoing = handler.s3.objects[("intake", "reports/drifted.csv.json")].decode() + json.dumps(handler.sns.messages)
    for value in ("Hosp. A", "Hospital A", "Readmission", "THA/TKA"):
        assert value not in outgoing
    raw = pd.read_csv(path, dtype=object, keep_default_na=False)
    values = {v for col in raw.columns for v in raw[col].unique() if len(str(v)) >= 6}
    assert [v for v in values if v in outgoing] == []
    for column in drift["columns"]:
        assert set(column) == {"column", "status", "kind", "shift_psi", "p_adjusted", "blanks_before",
                               "blanks_after", "new_category_share", "new_values", "rows_before", "rows_after",
                               "decision"}


@pytest.mark.parametrize("seed, n_stays", [(5, 300), (13, 1000), (42, 4000)])
def test_stable_batch_does_not_alert(handler, tmp_path, seed, n_stays):
    """Fresh batches from the same process, including small ones where sparse columns are noisy."""
    path = stable_copy(tmp_path, seed, n_stays)
    report = handler.check_file(path, SYNTHETIC)
    assert report["drift"]["status"] == "ok", report["drift"]["alerts"]
    subject, body = handler.summary(report)
    assert "DRIFT" not in subject and "no alerts" in body


def test_upload_never_overwrites_the_reference(handler, tmp_path):
    """Uploads, even one named like the reference, leave it byte-for-byte unchanged."""
    before = handler.s3.objects[REFERENCE]
    drifted = drifted_copy(tmp_path).read_bytes()
    for key in ("incoming/drifted.csv", "incoming/reference/discharges.csv", "incoming/discharges.csv"):
        handler.s3.objects[("intake", key)] = drifted
        handler.handler(s3_event("intake", key), None)
    assert handler.s3.objects[REFERENCE] == before
    assert all(bucket == "intake" and key.startswith("reports/") for bucket, key in handler.s3.writes)


def test_missing_reference_skips_only_the_drift_check(handler):
    del handler.s3.objects[REFERENCE]
    handler.s3.objects[("intake", "incoming/a.csv")] = SYNTHETIC.read_bytes()
    result = handler.handler(s3_event("intake", "incoming/a.csv"), None)
    assert result["checked"][0]["status"] == "passed"
    report = json.loads(handler.s3.objects[("intake", "reports/a.csv.json")])
    assert report["drift"] == {"status": "skipped", "reason": "reference not available (AccessDenied)"}
    assert "Drift check skipped" in handler.sns.messages[0]["Message"]


def test_a_failing_drift_check_does_not_stop_the_data_check(handler, monkeypatch):
    def boom(*_):
        raise ValueError("Hospital A")   # an error message could hold a value, so it isn't reported
    monkeypatch.setattr(handler.drift, "check", boom)
    report = handler.check_file(SYNTHETIC, SYNTHETIC)
    assert report["status"] == "passed"
    assert report["drift"] == {"status": "error", "reason": "drift check failed (ValueError)"}


def test_unreadable_and_skipped_files_get_no_drift_section(handler, tmp_path):
    (tmp_path / "wrong.csv").write_text("a,b\n1,2\n")
    for name in ("wrong.csv",):
        handler.s3.objects[("intake", f"incoming/{name}")] = (tmp_path / name).read_bytes()
        handler.handler(s3_event("intake", f"incoming/{name}"), None)
        assert "drift" not in json.loads(handler.s3.objects[("intake", f"reports/{name}.json")])


# ---------- The alert policy (drift_policy.py), one known answer per rule ----------

def row(**changes):
    base = {"column": "hospital", "status": "stable", "kind": "category", "shift_psi": 0.01, "p_adjusted": 0.9,
            "blanks_before": 0.0, "blanks_after": 0.0, "new_category_share": 0.0, "new_values": 0}
    return {**base, **changes}


@pytest.mark.parametrize("changes, decision", [
    ({}, "ok"),
    ({"status": "removed"}, "alert"),
    ({"status": "added"}, "log"),
    ({"status": "major", "kind": "number → category"}, "alert"),
    ({"status": "stable", "kind": "date → empty", "blanks_before": 0.997, "blanks_after": 1.0}, "ok"),
    ({"status": "major", "blanks_after": 0.10}, "alert"),
    ({"status": "moderate", "blanks_after": 0.05}, "log"),
    ({"status": "major", "shift_psi": 0.30, "p_adjusted": 0.001}, "alert"),
    ({"status": "major", "shift_psi": 0.30, "p_adjusted": 0.15}, "log"),      # big but could be chance
    ({"status": "major", "shift_psi": 0.30, "p_adjusted": None}, "alert"),    # no test: assume real
    ({"status": "moderate", "shift_psi": 0.15, "p_adjusted": 0.001}, "log"),  # moderate is never alerted
    ({"status": "major", "new_values": 1, "new_category_share": 0.08, "p_adjusted": 0.0}, "alert"),
    ({"column": "readmit_provider_id", "status": "major", "new_values": 9, "new_category_share": 0.9,
      "p_adjusted": 0.0}, "log"),                                             # identifiers: always new
    ({"column": "readmit_provider_id", "status": "major", "blanks_after": 0.5}, "alert"),
])
def test_alert_policy(changes, decision):
    import drift_policy

    assert drift_policy.decide(row(**changes))[0] == decision


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

    assert not list(out.glob("matplotlib*")), "matplotlib must not be bundled (dstoolkit doesn't need it)"
    assert (out / "drift.py").exists() and (out / "drift_policy.py").exists()

    data = tmp_path / "upload.csv"
    shutil.copy(drifted_copy(tmp_path), data)
    # -I ignores the environment and the current directory: only the package and the stdlib/boto3
    code = (f"import sys, json, pathlib; sys.path.insert(0, {str(out)!r}); import handler, pandas; "
            f"assert pandas.__file__.startswith({str(out)!r}); "
            f"print(json.dumps(handler.check_file(pathlib.Path({str(data)!r}), pathlib.Path({str(SYNTHETIC)!r}))))")
    run = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True, text=True,
                         env={"AWS_DEFAULT_REGION": "us-east-1"})
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout.strip().splitlines()[-1])
    assert report["status"] in ("passed", "failed")
    assert report["drift"]["status"] == "alert"   # dstoolkit and SciPy work from the package alone
