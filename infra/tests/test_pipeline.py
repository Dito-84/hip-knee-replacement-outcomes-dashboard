"""The deploy pipeline's own safety rails: required settings and a pinned toolchain."""

import json
import re
from pathlib import Path

import pytest

from dashboard_infra import bundling
from dashboard_infra.settings import MissingAlertEmail, alert_email

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def context(**values):
    return lambda key: values.get(key)


# ---------- A synth without the alert email stops, instead of removing the alerts ----------

def test_alert_email_is_returned_when_given():
    assert alert_email(context(alert_email=" me@example.com ")) == "me@example.com"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_missing_alert_email_stops_the_synth(value):
    """An empty ALERT_EMAIL secret used to synthesize a stack with no email subscription or budget."""
    with pytest.raises(MissingAlertEmail, match="No alert_email"):
        alert_email(context(alert_email=value))


def test_a_synth_that_wont_be_deployed_can_say_so():
    assert alert_email(context(allow_no_alerts="true")) is None
    assert alert_email(context(allow_no_alerts="True")) is None
    with pytest.raises(MissingAlertEmail):
        alert_email(context(allow_no_alerts="yes"))   # only the exact word, so it's never set by accident


def test_something_that_isnt_an_email_address_stops_the_synth():
    with pytest.raises(MissingAlertEmail, match="doesn't look like"):
        alert_email(context(alert_email="ALERT_EMAIL"))


def test_only_ci_synth_check_opts_out_of_alerts():
    """The flag must never reach a workflow that deploys or diffs against the live stack."""
    for wf in WORKFLOWS.glob("*.yml"):
        uses_flag = "allow_no_alerts" in wf.read_text()
        assert uses_flag == (wf.name == "ci.yml"), wf.name
    assert (ROOT / "infra" / "cdk.json").read_text().count("allow_no_alerts") == 0


def test_every_cdk_command_in_a_workflow_that_reads_the_stack_passes_the_alert_email():
    for name in ("stack-diff.yml", "deploy-stack.yml"):
        text = (WORKFLOWS / name).read_text()
        commands = re.findall(r"npx --no-install cdk (?:diff|deploy)(?:[^\n]*\\\n)*[^\n]*", text)
        assert commands, name
        for command in commands:
            assert '-c alert_email="$ALERT_EMAIL"' in command, (name, command)


# ---------- The toolchain the approved deploy job runs is pinned ----------

def test_actions_are_pinned_to_a_commit():
    for wf in WORKFLOWS.glob("*.yml"):
        for ref in re.findall(r"uses:\s*(\S+)", wf.read_text()):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{wf.name}: {ref} isn't pinned by SHA"


def test_the_cdk_cli_is_pinned_and_never_fetched_at_run_time():
    package = json.loads((ROOT / "infra" / "package.json").read_text())
    version = package["devDependencies"]["aws-cdk"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), "exact version, no range"
    lock = json.loads((ROOT / "infra" / "package-lock.json").read_text())
    assert lock["packages"]["node_modules/aws-cdk"]["version"] == version
    assert lock["packages"]["node_modules/aws-cdk"]["integrity"].startswith("sha512-")
    for wf in WORKFLOWS.glob("*.yml"):
        # Commands only: a message telling you what to run on the laptop may name the CLI
        commands = [line for line in wf.read_text().splitlines() if "echo" not in line]
        assert not [c for c in commands if "aws-cdk@" in c or "npx --yes" in c], wf.name


def test_python_requirements_for_synth_and_deploy_are_exact():
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in (ROOT / "infra" / name).read_text().splitlines():
            line = line.split("#")[0].strip()
            if line and not line.startswith("-r"):
                assert re.fullmatch(r"[A-Za-z0-9_.-]+==[\w.]+", line), f"{name}: {line!r} isn't an exact pin"


def test_the_deploy_job_installs_nothing_unpinned():
    text = (WORKFLOWS / "deploy-stack.yml").read_text()
    for install in re.findall(r"pip install ([^\n]+)", text):
        assert all(arg.startswith("-r") or arg.endswith(".txt") for arg in install.split()), install


# ---------- The Lambda package is tested on the Python it runs on ----------

def test_ci_builds_the_lambda_package_on_the_lambda_runtime_python():
    ci = (WORKFLOWS / "ci.yml").read_text()
    assert bundling.RUNTIME.name == f"python{bundling.PYTHON_VERSION}"
    assert f'python-version: "{bundling.PYTHON_VERSION}"' in ci
    assert "-k bundled --bundle" in ci
