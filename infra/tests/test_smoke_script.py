"""scripts/smoke_upload.py's check 8: the live upload page against the stack's outputs."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import smoke_upload  # noqa: E402

OUT = {"UploadApiUrl": "https://abc123.execute-api.us-east-1.amazonaws.com/",
       "AuthDomain": "https://example-prefix.auth.us-east-1.amazoncognito.com",
       "UserPoolClientId": "1234567890abcdefghij12", "IntakeBucketName": "example-intake-bucket"}
CONFIG = {"apiUrl": "https://abc123.execute-api.us-east-1.amazonaws.com",
          "authDomain": "https://example-prefix.auth.us-east-1.amazoncognito.com",
          "clientId": "1234567890abcdefghij12", "bucket": "example-intake-bucket", "region": "us-east-1"}


def page(config):
    settings = {k: v for k, v in config.items() if k != "bucket"}
    csp = (f'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; connect-src '
           f'{config["apiUrl"]} https://{config["bucket"]}.s3.amazonaws.com">')
    return f"{csp}<script>\nconst CONFIG = {json.dumps(settings)};\nconst MAX_BYTES = 1;\n</script>"


def test_a_page_built_from_the_outputs_matches():
    assert smoke_upload.config_mismatches(smoke_upload.page_config(page(CONFIG)), OUT) == []


def test_a_stale_bucket_in_the_csp_is_named_too():
    stale = {**CONFIG, "bucket": "old-intake-bucket"}
    assert smoke_upload.config_mismatches(smoke_upload.page_config(page(stale)), OUT) == ["UPLOAD_BUCKET"]


def test_a_stale_client_id_is_named_by_its_github_variable():
    stale = {**CONFIG, "clientId": "oldoldoldoldoldoldold1"}
    assert smoke_upload.config_mismatches(smoke_upload.page_config(page(stale)), OUT) == ["UPLOAD_CLIENT_ID"]


def test_a_page_built_without_settings_is_reported():
    html = "<script>\nconst CONFIG = /*__CONFIG__*/null;\n</script>"
    assert smoke_upload.page_config(html) is None
    assert "no settings" in smoke_upload.config_mismatches(None, OUT)[0]


def test_the_built_page_embeds_its_settings_where_the_smoke_test_reads_them(tmp_path, monkeypatch):
    """build.py and the smoke test must agree on the format, or check 8 would always fail."""
    sys.path.insert(0, str(ROOT))
    import build
    env = {"UPLOAD_API_URL": CONFIG["apiUrl"], "UPLOAD_AUTH_DOMAIN": CONFIG["authDomain"],
           "UPLOAD_CLIENT_ID": CONFIG["clientId"], "UPLOAD_BUCKET": CONFIG["bucket"]}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    build.main(["--out", str(tmp_path)])
    html = (tmp_path / "upload.html").read_text()
    assert smoke_upload.config_mismatches(smoke_upload.page_config(html), OUT) == []


class FakeCloudWatch:
    def __init__(self):
        self.set = []

    def get_paginator(self, name):
        assert name == "describe_alarms"

        class P:
            def paginate(self, **kw):
                assert kw == {"AlarmNamePrefix": "HipKneeDashboard-", "AlarmTypes": ["MetricAlarm"]}
                return [{"MetricAlarms": [
                    {"AlarmName": "HipKneeDashboard-A", "StateValue": "OK", "AlarmActions": ["arn:topic"]},
                    {"AlarmName": "HipKneeDashboard-B", "StateValue": "OK", "AlarmActions": []}]}]
        return P()

    def set_alarm_state(self, **kw):
        self.set.append(kw)


def test_alarm_drill_lists_by_default_and_only_sets_what_it_is_told(monkeypatch, capsys):
    import alarm_drill
    fake = FakeCloudWatch()
    monkeypatch.setattr(alarm_drill.boto3, "Session", lambda **kw: type("S", (), {"client": lambda s, n: fake})())
    alarm_drill.main([])
    assert fake.set == [] and "NO ACTION" in capsys.readouterr().out   # an alarm that can't email is flagged
    alarm_drill.main(["--alarm", "HipKneeDashboard-A"])
    assert [s["AlarmName"] for s in fake.set] == ["HipKneeDashboard-A"] and fake.set[0]["StateValue"] == "ALARM"
    alarm_drill.main(["--all"])
    assert len(fake.set) == 3
