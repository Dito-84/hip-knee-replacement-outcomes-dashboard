"""scripts/publish_reference.py publishes only the committed synthetic dataset."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_reference  # noqa: E402


def test_dry_run_names_the_committed_file_and_publishes_nothing(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)          # any AWS call would fail the test
    publish_reference.main(["--bucket", "example-reference", "--dry-run"])
    out = capsys.readouterr().out
    assert "data/synthetic_discharges.csv at commit" in out
    assert "s3://example-reference/reference/discharges.csv" in out
    assert "nothing published" in out


def test_refuses_a_modified_reference(monkeypatch):
    def fake_git(*args):
        return " M data/synthetic_discharges.csv" if args[0] == "status" else "tracked"
    monkeypatch.setattr(publish_reference, "git", fake_git)
    with pytest.raises(SystemExit, match="uncommitted changes"):
        publish_reference.main(["--bucket", "b", "--dry-run"])


def test_refuses_an_untracked_reference(monkeypatch):
    monkeypatch.setattr(publish_reference, "git", lambda *args: "")
    with pytest.raises(SystemExit, match="isn't tracked"):
        publish_reference.main(["--bucket", "b", "--dry-run"])


def test_key_matches_the_stack():
    stack = (Path(__file__).resolve().parents[1] / "infra" / "dashboard_infra" / "stack.py").read_text()
    assert f'REFERENCE_KEY = "{publish_reference.REFERENCE_KEY}"' in stack
