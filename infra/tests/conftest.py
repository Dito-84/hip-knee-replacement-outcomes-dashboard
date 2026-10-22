import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # dashboard_infra/


def pytest_addoption(parser):
    parser.addoption("--bundle", action="store_true", help="also build and run the real Lambda package")
