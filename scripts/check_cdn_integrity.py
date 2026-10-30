"""Check that every CDN script in the dashboard template matches its integrity hash.

    python scripts/check_cdn_integrity.py

Downloads each <script src=... integrity=...> in dashboard/template.html and
compares its SHA-384 hash with the one in the template. If they differ, the
browser would refuse to run the script and the page would break, so CI runs this
before anything is deployed. Needs internet access.
"""

import base64
import hashlib
import re
import sys
import urllib.request
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "dashboard" / "template.html"
TAG = re.compile(r'<script src="([^"]+)"\s+integrity="(sha384-[^"]+)"', re.S)


def sri_hash(data: bytes) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()


def main() -> int:
    tags = TAG.findall(TEMPLATE.read_text(encoding="utf-8"))
    if not tags:
        print("No CDN scripts with an integrity hash found in the template.")
        return 1
    failed = 0
    for url, expected in tags:
        with urllib.request.urlopen(url, timeout=30) as response:
            actual = sri_hash(response.read())
        ok = actual == expected
        failed += not ok
        print(f"{'OK  ' if ok else 'FAIL'} {url}")
        if not ok:
            print(f"     template {expected}\n     actual   {actual}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
