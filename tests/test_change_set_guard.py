"""The deploy's replacement guard, on made-up change sets where the right answer is known."""

import json
import sys

import pytest

from helpers import ROOT

sys.path.insert(0, str(ROOT / "scripts"))
import check_change_set as guard  # noqa: E402


def change(kind, action="Modify", replacement="False", name="Thing"):
    rc = {"Action": action, "LogicalResourceId": name, "ResourceType": kind}
    if action == "Modify":
        rc["Replacement"] = replacement
    return {"Type": "Resource", "ResourceChange": rc}


@pytest.mark.parametrize("changes", [
    [],
    [change("AWS::Lambda::Function", replacement="True")],     # replacing a function loses nothing
    [change("AWS::S3::Bucket")],                               # an in-place bucket change (e.g. CORS)
    [change("AWS::IAM::Policy", action="Remove")],
    [change("AWS::Cognito::UserPool", action="Add")],
])
def test_safe_change_sets_pass(changes):
    assert guard.violations(changes) == []


@pytest.mark.parametrize("kind", sorted(guard.STATEFUL))
def test_replacing_any_stateful_resource_is_refused(kind):
    [problem] = guard.violations([change(kind, replacement="True", name="Res")])
    assert problem.startswith("Res: would REPLACE")


@pytest.mark.parametrize("kind", ["AWS::S3::Bucket", "AWS::Cognito::UserPool", "AWS::KMS::Key"])
def test_removing_a_stateful_resource_is_refused(kind):
    [problem] = guard.violations([change(kind, action="Remove")])
    assert "would REMOVE" in problem


def test_conditional_replacement_is_refused_too():
    [problem] = guard.violations([change("AWS::DynamoDB::Table", replacement="Conditional")])
    assert "might replace" in problem


def test_exit_codes(tmp_path, capsys):
    path = tmp_path / "changes.json"
    path.write_text(json.dumps([change("AWS::Lambda::Function"), change("AWS::S3::Bucket")]))
    assert guard.main(["--stack", "S", "--change-set", "c", "--from-file", str(path)]) == 0

    path.write_text(json.dumps([change("AWS::Cognito::UserPool", replacement="True", name="Pool")]))
    assert guard.main(["--stack", "S", "--change-set", "c", "--from-file", str(path)]) == 1
    out = capsys.readouterr().out
    assert "::error title=Replacement guard::Pool: would REPLACE" in out and "Refused" in out
    # A deliberate replacement, run by hand with the override
    assert guard.main(["--stack", "S", "--change-set", "c", "--from-file", str(path), "--allow-replacement"]) == 0


def test_no_change_set_means_nothing_to_deploy(monkeypatch):
    monkeypatch.setattr(guard, "fetch_changes", lambda *_: None)
    assert guard.main(["--stack", "S", "--change-set", "c"]) == guard.NO_CHANGES
