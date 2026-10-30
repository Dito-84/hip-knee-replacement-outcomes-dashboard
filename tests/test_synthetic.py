"""The synthetic dataset: reproducible, in the source format, current, and clinically plausible."""

import pandas as pd

from helpers import ROOT
from outcomes import synthetic
from outcomes.load import load
from outcomes.metrics import build_stays, summarize


def test_synthetic_is_reproducible():
    a = synthetic.generate(n_stays=200)
    b = synthetic.generate(n_stays=200)
    pd.testing.assert_frame_equal(a, b)


def test_synthetic_matches_source_format(tmp_path):
    path = tmp_path / "synthetic.csv"
    synthetic.generate(n_stays=300).to_csv(path, index=False)
    df = load(path)
    stays, _ = build_stays(df)
    assert len(stays) == 300
    rate = stays["complication_all"].mean()
    assert 0 < rate < 0.15


def test_committed_synthetic_file_is_current():
    committed = pd.read_csv(ROOT / "data" / "synthetic_discharges.csv", dtype=str, keep_default_na=False)
    fresh = synthetic.generate().astype(str)
    pd.testing.assert_frame_equal(committed.reset_index(drop=True), fresh.reset_index(drop=True))


def test_synthetic_in_stay_complications_lengthen_the_stay():
    stays, comps = build_stays(load(ROOT / "data" / "synthetic_discharges.csv"))
    los = dict(zip(*[summarize(stays, comps)["los_by_status"][k] for k in ("status", "avg_los")]))
    assert los["Complication during stay"] > los["No complication"] + 1


def test_synthetic_under_65_are_excluded():
    stays, _ = build_stays(load(ROOT / "data" / "synthetic_discharges.csv"))
    under = stays[stays["age"] < 65]
    assert len(under) > 0
    assert under["excluded"].all()
