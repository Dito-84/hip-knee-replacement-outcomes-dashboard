"""The upload page build: only with all four settings, only for synthetic data, and a strict CSP."""

import json
import re
import shutil
import subprocess

import pytest

import build
from helpers import ROOT
from outcomes import samples, synthetic_guard

CONFIG = {
    "UPLOAD_API_URL": "https://abc123def4.execute-api.us-east-1.amazonaws.com",
    "UPLOAD_AUTH_DOMAIN": "https://hip-knee-dashboard-dito84.auth.us-east-1.amazoncognito.com",
    "UPLOAD_CLIENT_ID": "1a2b3c4d5e6f7g8h9i0j1k2l3m",
    "UPLOAD_BUCKET": "hipkneedashboard-intakebucket-abc",
}


def test_no_settings_means_no_upload_page(tmp_path, monkeypatch):
    for name in CONFIG:
        monkeypatch.delenv(name, raising=False)
    build.main(["--out", str(tmp_path)])
    assert (tmp_path / "index.html").exists()
    assert not (tmp_path / "upload.html").exists() and not (tmp_path / "samples").exists()


def test_settings_build_the_page_and_samples(tmp_path, monkeypatch):
    for name, value in CONFIG.items():
        monkeypatch.setenv(name, value)
    build.main(["--out", str(tmp_path)])
    html = (tmp_path / "upload.html").read_text()
    assert sorted(p.name for p in (tmp_path / "samples").iterdir()) == [
        "sample-clean.csv", "sample-drift.csv", "sample-no-marker.csv", "sample-quality-problems.csv"]
    config = json.loads(re.search(r"const CONFIG = (\{.*?\});\n", html).group(1))
    assert config["apiUrl"] == CONFIG["UPLOAD_API_URL"] and config["clientId"] == CONFIG["UPLOAD_CLIENT_ID"]
    assert [s["file"] for s in config["samples"]][0] == "sample-clean.csv"


def test_upload_page_csp_allows_only_the_api_sign_in_and_bucket(tmp_path, monkeypatch):
    for name, value in CONFIG.items():
        monkeypatch.setenv(name, value)
    build.main(["--out", str(tmp_path)])
    html = (tmp_path / "upload.html").read_text()
    policy = re.search(r'Content-Security-Policy" content="([^"]+)"', html).group(1)
    rules = dict(rule.split(" ", 1) for rule in policy.split("; "))
    assert rules["default-src"] == "'none'" and rules["form-action"] == "'none'"
    assert rules["script-src"].startswith("'sha256-") and " " not in rules["script-src"]   # one script, by hash
    assert rules["style-src"].startswith("'sha256-") and "unsafe" not in policy
    assert rules["connect-src"].split() == [
        CONFIG["UPLOAD_API_URL"], CONFIG["UPLOAD_AUTH_DOMAIN"],
        f"https://{CONFIG['UPLOAD_BUCKET']}.s3.amazonaws.com",
        f"https://{CONFIG['UPLOAD_BUCKET']}.s3.us-east-1.amazonaws.com"]
    assert "localStorage" not in html   # tokens stay in memory


@pytest.mark.parametrize("name, value", [
    ("UPLOAD_API_URL", "https://evil.example.com"),
    ("UPLOAD_API_URL", "https://abc.execute-api.us-east-1.amazonaws.com; script-src *"),
    ("UPLOAD_AUTH_DOMAIN", "http://hip-knee-dashboard-dito84.auth.us-east-1.amazoncognito.com"),
    ("UPLOAD_CLIENT_ID", "x"),
    ("UPLOAD_BUCKET", "Bad_Bucket"),
    ("UPLOAD_BUCKET", ""),             # some set, one missing
])
def test_settings_that_dont_look_like_stack_outputs_stop_the_build(name, value):
    with pytest.raises(SystemExit):
        build.upload_config({**CONFIG, name: value})


def test_upload_page_is_never_built_alongside_real_data(tmp_path, monkeypatch):
    for name, value in CONFIG.items():
        monkeypatch.setenv(name, value)
    other = tmp_path / "copy.csv"
    shutil.copy(ROOT / "data" / "synthetic_discharges.csv", other)   # any file but the committed synthetic one
    out = tmp_path / "site"
    build.main(["--data", str(other), "--out", str(out)])
    assert (out / "index.html").exists() and not (out / "upload.html").exists()


def test_samples_are_reproducible_and_all_but_one_are_marked(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    samples.write_samples(a)
    samples.write_samples(b)
    for f in a.iterdir():
        assert f.read_bytes() == (b / f.name).read_bytes()
        expected = synthetic_guard.NO_MARKER_COLUMN if f.name == "sample-no-marker.csv" else None
        assert synthetic_guard.check(f) == expected


# The page refuses files in the browser with the same rules as outcomes/synthetic_guard.py
GUARD_CASES = [
    ("a.xlsx", "Hospital,Synthetic Marker\nHospital A,{M}\n", False),
    ("a.csv", "Hospital,Measure\nHospital A,x\n", False),
    ("a.csv", 'Hospital,"Synthetic Marker"\n"Hospital A",{M}\n\n', True),
    ("a.csv", "Hospital,Synthetic Marker\r\nGeneral Hospital,{M}\r\n", False),
    ("a.csv", "Hospital,Synthetic Marker\nHospital B,\n", False),
    ("a.csv", '﻿Hospital,"Note, with comma",Synthetic Marker\nHospital C,"x,""y""",{M}\n', True),
    ("a.csv", "Exported 2023\nHospital,Synthetic Marker\nHospital A,{M}\n", False),
    ("a.csv", "Hospital,Synthetic Marker\nHospital A,{M}\nHospital A,{M},extra\nHospital C\n", False),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_browser_guard_agrees_with_the_python_guard(tmp_path):
    html = (ROOT / "dashboard" / "upload.html").read_text()
    js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    functions = js[js.index("const MARKER_COLUMN"):js.index("const MAX_BYTES")] + \
        js[js.index("function parseCsv"):js.index("function api(")]
    cases = [(name, text.replace("{M}", synthetic_guard.MARKER_VALUE)) for name, text, _ in GUARD_CASES]
    script = tmp_path / "guard.js"
    script.write_text(functions + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(([n, t]) => screen(n, t))));")
    browser = json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, check=True).stdout)

    for (name, text), accepted, (_, _, expected) in zip(cases, browser, GUARD_CASES, strict=True):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8", newline="")
        assert (synthetic_guard.check(path) is None) == expected, (name, text)
        assert (accepted is None) == expected, (name, text, accepted)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_sign_in_errors_never_show_text_from_the_address_bar(tmp_path):
    """A crafted link could otherwise put any message on the site's own page."""
    html = (ROOT / "dashboard" / "upload.html").read_text()
    js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    functions = js[js.index("const EXPIRY_MESSAGES"):js.index("function signOut")]
    expired = "PreAuthentication failed with error This demo account has expired. Ask for a new invitation."
    cases = [
        {"error": "access_denied", "error_description": expired},
        {"error": "access_denied", "error_description": "Your account is locked. Call +1 555 0100 to unlock it."},
        {"error": "<b>call us</b>", "error_description": ""},
        {"error": "invalid_request"},
    ]
    script = tmp_path / "errors.js"
    script.write_text(functions + f"\nconsole.log(JSON.stringify({json.dumps(cases)}"
                      ".map((c) => signInError(new URLSearchParams(c)))));")
    shown = json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, check=True).stdout)
    assert shown == [
        "Sign-in failed: This demo account has expired. Ask for a new invitation.",
        "Sign-in failed (access_denied).",
        "Sign-in failed.",
        "Sign-in failed (invalid_request).",
    ]


def test_the_expiry_messages_the_page_knows_are_the_triggers_own():
    trigger = (ROOT / "infra" / "lambda" / "pre_auth" / "handler.py").read_text()
    page = (ROOT / "dashboard" / "upload.html").read_text()
    for message in re.findall(r'AccountExpired\("([^"]+)"\)', trigger):
        assert f'"{message}"' in page, message
