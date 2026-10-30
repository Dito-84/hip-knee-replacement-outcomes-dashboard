"""Statistical helpers, each checked against a textbook value or a hand calculation,
and each 95% interval checked by simulation to contain the truth about 95% of the time."""

import numpy as np
import pytest

from outcomes.stats import binomial_quantile, mantel_haenszel, odds_ratio, poisson_ratio_interval, wilson_interval


def test_wilson_interval_contains_rate():
    low, high = wilson_interval(3, 100)
    assert 0 < low < 0.03 < high < 0.1
    assert wilson_interval(0, 0) == (None, None)


def test_oe_interval_matches_exact_poisson_limits():
    # Exact Poisson 95% limits: 0 events -> (0, 3.689); 10 events -> (4.795, 18.39)
    low, high = poisson_ratio_interval(0, 1)
    assert low == 0.0 and high == pytest.approx(3.689, rel=0.01)
    low, high = poisson_ratio_interval(10, 10)
    assert low == pytest.approx(0.4795, rel=0.01)
    assert high == pytest.approx(1.839, rel=0.01)
    assert poisson_ratio_interval(3, 0) == (None, None)


def test_odds_ratio_and_woolf_interval():
    # 10/100 hips vs 5/100 knees: OR = (10*95)/(90*5) = 2.111, Woolf 95% CI 0.695 to 6.42
    ratio, low, high = odds_ratio(10, 90, 5, 95)
    assert ratio == pytest.approx(2.111, rel=0.001)
    assert low == pytest.approx(0.6946, rel=0.01)
    assert high == pytest.approx(6.416, rel=0.01)
    # A zero cell gets 0.5 added to every cell: (0.5*5.5)/(10.5*5.5) = 0.0476
    assert odds_ratio(0, 10, 5, 5)[0] == pytest.approx(0.5 / 10.5)
    assert odds_ratio(0, 0, 5, 5) == (None, None, None)


def test_mantel_haenszel_removes_confounding_by_age():
    # Within each age group hips and knees have the same odds (OR 1), but hips are mostly older,
    # and older patients have more complications.
    young = (1, 9, 4, 36)       # hips 1/10, knees 4/40 -> OR (1*36)/(9*4) = 1
    old = (20, 20, 5, 5)        # hips 20/40, knees 5/10 -> OR (20*5)/(20*5) = 1
    crude = odds_ratio(21, 29, 9, 41)[0]          # pooled: (21*41)/(29*9) = 3.30
    assert crude == pytest.approx(861 / 261)
    assert mantel_haenszel([young, old])[0] == pytest.approx(1.0)


def test_mantel_haenszel_with_one_stratum_matches_woolf():
    assert mantel_haenszel([(10, 90, 5, 95)]) == pytest.approx(odds_ratio(10, 90, 5, 95))
    assert mantel_haenszel([(0, 10, 0, 10)]) == (None, None, None)


def test_binomial_quantile_exact_values():
    # Binomial(100, 0.03): P(X <= 0) = 0.048, P(X <= 6) = 0.969, P(X <= 7) = 0.989, P(X <= 8) = 0.996
    assert binomial_quantile(100, 0.03, 0.025) == 0
    assert binomial_quantile(100, 0.03, 0.975) == 7
    assert binomial_quantile(100, 0.03, 0.999) == 9
    assert binomial_quantile(0, 0.03, 0.975) == 0
    assert binomial_quantile(50, 0.0, 0.975) == 0
    assert binomial_quantile(50, 1.0, 0.025) == 50


# ---------- coverage: do the 95% intervals contain the truth about 95% of the time? ----------
# Each draws many samples from a known truth (seeded, so the result is fixed) and counts how
# often the interval contains it. The bounds allow for simulation noise.

def test_wilson_interval_coverage():
    rng = np.random.default_rng(1)
    p, n = 0.03, 500
    hits = [lo <= p <= hi for lo, hi in (wilson_interval(int(k), n) for k in rng.binomial(n, p, 4000))]
    assert 0.93 <= np.mean(hits) <= 0.97


def test_byar_interval_coverage():
    rng = np.random.default_rng(2)
    expected = 20.0             # true O/E = 1: observed counts are Poisson with mean 20
    intervals = (poisson_ratio_interval(int(o), expected) for o in rng.poisson(expected, 4000))
    hits = [lo <= 1.0 <= hi for lo, hi in intervals]
    assert 0.93 <= np.mean(hits) <= 0.97


def test_woolf_interval_coverage():
    rng = np.random.default_rng(3)
    p1, p2, n = 0.05, 0.03, 1000
    true_or = (p1 / (1 - p1)) / (p2 / (1 - p2))
    hits = []
    for a, c in zip(rng.binomial(n, p1, 4000), rng.binomial(n, p2, 4000)):
        _, lo, hi = odds_ratio(int(a), int(n - a), int(c), int(n - c))
        hits.append(lo <= true_or <= hi)
    assert 0.93 <= np.mean(hits) <= 0.97
