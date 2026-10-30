"""Statistical helpers used by the measures: intervals, odds ratios, pooled odds ratios.

Pure functions of counts, with no pandas, so each can be checked against a
textbook example (see tests/test_stats.py).
"""

import numpy as np


def wilson_interval(successes, n, z=1.96):
    """95% confidence interval for a proportion (stable for small counts)."""
    if n == 0:
        return None, None
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return float(max(0.0, center - half)), float(min(1.0, center + half))


def odds_ratio(a, b, c, d, z=1.96):
    """Odds ratio for a 2x2 table with a 95% CI (Woolf's log method).

    a, b: exposed with / without the outcome; c, d: unexposed with / without.
    If any cell is zero, 0.5 is added to every cell (Haldane-Anscombe correction).
    """
    if min(a + b, c + d) == 0 or min(a + c, b + d) == 0:
        return None, None, None
    if 0 in (a, b, c, d):
        a, b, c, d = a + 0.5, b + 0.5, c + 0.5, d + 0.5
    ratio = (a * d) / (b * c)
    se = np.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
    return float(ratio), float(np.exp(np.log(ratio) - z * se)), float(np.exp(np.log(ratio) + z * se))


def mantel_haenszel(tables, z=1.96):
    """Odds ratio pooled across strata, with a 95% CI (Mantel-Haenszel, Robins-Breslow-Greenland variance).

    `tables` is a list of (a, b, c, d) 2x2 tables, one per stratum, laid out as in
    odds_ratio(). Strata with fewer than two people add nothing. Returns
    (None, None, None) when the pooled ratio is undefined (no discordant information).
    """
    sum_r = sum_s = sum_pr = sum_ps_qr = sum_qs = 0.0
    for a, b, c, d in tables:
        n = a + b + c + d
        if n < 2:
            continue
        r, s_ = a * d / n, b * c / n
        p, q = (a + d) / n, (b + c) / n
        sum_r += r
        sum_s += s_
        sum_pr += p * r
        sum_ps_qr += p * s_ + q * r
        sum_qs += q * s_
    if sum_r == 0 or sum_s == 0:
        return None, None, None
    ratio = sum_r / sum_s
    var = sum_pr / (2 * sum_r ** 2) + sum_ps_qr / (2 * sum_r * sum_s) + sum_qs / (2 * sum_s ** 2)
    se = np.sqrt(var)
    return float(ratio), float(np.exp(np.log(ratio) - z * se)), float(np.exp(np.log(ratio) + z * se))


def poisson_ratio_interval(observed, expected, z=1.96):
    """95% interval for observed/expected, treating observed as Poisson (Byar's approximation)."""
    if expected == 0:
        return None, None
    o = observed
    low = 0.0 if o == 0 else o * (1 - 1 / (9 * o) - z / (3 * np.sqrt(o))) ** 3
    high = (o + 1) * (1 - 1 / (9 * (o + 1)) + z / (3 * np.sqrt(o + 1))) ** 3
    return float(low / expected), float(high / expected)
