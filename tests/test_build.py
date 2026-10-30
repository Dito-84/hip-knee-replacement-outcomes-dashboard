"""build.py: data checks before every build."""

import pytest

from helpers import ROOT, make_records, stay


def test_build_stops_on_data_errors_unless_skipped(tmp_path, capsys):
    import build
    path = tmp_path / "bad.csv"
    make_records([
        stay(dob="01/01/1950", had_complication="Yes", complication="Infection",
             during_index_stay="No", complication_admit_date="03/01/2020"),
        stay(dob="02/02/1950"),
    ]).to_csv(path, index=False)
    out = tmp_path / "site"
    with pytest.raises(SystemExit) as stopped:
        build.main(["--data", str(path), "--out", str(out)])
    assert "Stopped" in str(stopped.value)
    assert not (out / "index.html").exists()
    build.main(["--data", str(path), "--out", str(out), "--skip-checks"])
    assert (out / "index.html").exists()


def test_page_has_a_content_security_policy_matching_its_script(tmp_path):
    import base64
    import hashlib
    import re

    import build
    build.main(["--out", str(tmp_path)])
    html = (tmp_path / "index.html").read_text()
    policy = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', html).group(1)
    assert "default-src 'none'" in policy
    assert "'unsafe-inline'" not in policy.split("script-src")[1].split(";")[0]   # no inline scripts but ours
    inline = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert len(inline) == 1
    digest = base64.b64encode(hashlib.sha256(inline[0].encode()).digest()).decode()
    assert f"'sha256-{digest}'" in policy
    assert re.search(r'<script src="https://cdn\.jsdelivr\.net/[^"]+"\s+integrity="sha384-', html)


def test_sri_hash_matches_the_standard_encoding():
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_cdn_integrity import sri_hash
    # SHA-384 of empty input, base64-encoded, as in the SRI spec examples
    assert sri_hash(b"") == "sha384-OLBgp1GsljhM2TJ+sbHjaiH9txEUvgdDTAzHv2P24donTt6/529l+9Ua0vFImLlb"
