"""Package the data-check Lambda: handler + the repo's outcomes/ package + pandas.

pandas and numpy are compiled libraries, so the wheels must match Lambda
(Linux, ARM64, Python 3.14), not the computer running `cdk deploy`. pip can
download wheels for another platform, so this works on a Mac without Docker.
Bundling always runs locally; there is no Docker fallback, so a pip failure
stops the deploy with pip's own error message.
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import jsii
from aws_cdk import AssetHashType, BundlingOptions, DockerImage, ILocalBundling
from aws_cdk import aws_lambda as lambda_

REPO_ROOT = Path(__file__).resolve().parents[2]
HANDLER_DIR = REPO_ROOT / "infra" / "lambda" / "data_check"
OUTCOMES_DIR = REPO_ROOT / "outcomes"

RUNTIME = lambda_.Runtime.PYTHON_3_14
ARCHITECTURE = lambda_.Architecture.ARM_64
PYTHON_VERSION = "3.14"
PLATFORMS = ["manylinux_2_28_aarch64", "manylinux_2_17_aarch64", "manylinux2014_aarch64"]

# Test suites and caches that ship inside the wheels but are never used at runtime
STRIP = ["pandas/tests", "numpy/*/tests", "numpy/tests", "**/__pycache__"]


def _source_files():
    files = [p for p in HANDLER_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    files += [p for p in OUTCOMES_DIR.glob("*.py")]
    return sorted(files)


def source_hash() -> str:
    """Changes whenever the handler, the outcomes package or the pinned requirements change."""
    digest = hashlib.sha256()
    for path in _source_files():
        digest.update(str(path.relative_to(REPO_ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def assemble(output_dir: Path, *, python_version: str = PYTHON_VERSION, platforms: list[str] = PLATFORMS) -> None:
    """Install dependencies for Lambda's platform and copy the code into output_dir.

    The tests pass the local platform instead, so the package can be imported and run here.
    """
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--no-compile", "--disable-pip-version-check",
         "--no-warn-conflicts", "--target", str(output_dir), "--implementation", "cp",
         "--python-version", python_version, "--only-binary=:all:",
         *[arg for p in platforms for arg in ("--platform", p)],
         "-r", str(HANDLER_DIR / "requirements.txt")],
        check=True,
    )
    for pattern in STRIP:
        for path in output_dir.glob(pattern):
            shutil.rmtree(path, ignore_errors=True)
    shutil.copy2(HANDLER_DIR / "handler.py", output_dir / "handler.py")
    shutil.copytree(OUTCOMES_DIR, output_dir / "outcomes", ignore=shutil.ignore_patterns("__pycache__"),
                    dirs_exist_ok=True)


@jsii.implements(ILocalBundling)
class _LocalBundling:
    def try_bundle(self, output_dir: str, *args, **kwargs) -> bool:
        assemble(Path(output_dir))
        return True


def data_check_code() -> lambda_.Code:
    return lambda_.Code.from_asset(
        str(HANDLER_DIR),
        asset_hash_type=AssetHashType.CUSTOM,
        asset_hash=source_hash(),
        bundling=BundlingOptions(
            local=_LocalBundling(),
            # Required by CDK but never used: _LocalBundling always handles the bundling
            image=DockerImage.from_registry("local-bundling-only"),
        ),
    )
