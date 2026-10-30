"""Small-cell suppression for builds that will be shared.

CMS doesn't allow publishing a count of 1 to 10 people (its cell-size rule:
no cell under 11), or a figure from which such a count can be worked out.
`suppress(data, threshold)` takes the output of build_dashboard_data() and
replaces every count of 1 to threshold - 1, and every rate, interval, average
or ratio computed from one, with None (shown as "–" on the page). Zero is
shown, as the rule allows.

This is primary suppression only. Some hidden counts could still be worked out
by subtraction (a total minus the other cells), so review any build before
sharing it. Off by default: the synthetic data has no real people in it.
"""

import copy


def _small(n, threshold):
    return n is not None and 0 < n < threshold


def _hide_rate(rec, count_key, threshold, derived):
    """Hide `count_key` and its derived keys in one record when the count is small."""
    if _small(rec.get(count_key), threshold):
        rec[count_key] = None
        for key in derived:
            if key in rec:
                rec[key] = [None, None] if isinstance(rec[key], list) else None


def _suppress_kpis(k, threshold):
    if _small(k["stays"], threshold):
        for key in k:
            if key != "hospital":
                k[key] = None
        return
    _hide_rate(k, "complications", threshold, ["complication_rate", "complication_ci", "expected",
                                               "oe_ratio", "oe_ci", "adjusted_rate"])
    _hide_rate(k, "readmit30", threshold, ["readmit30_rate"])
    _hide_rate(k, "deaths", threshold, ["mortality_rate"])


def _suppress_series(counts, threshold):
    return [None if _small(n, threshold) else n for n in counts]


def _suppress_view(v, threshold):
    _suppress_kpis(v["kpis"], threshold)

    y = v["by_year"]
    for i, (n, c) in enumerate(zip(y["stays"], y["complications"])):
        if _small(n, threshold) or _small(c, threshold):
            y["complication_rate"][i], y["complication_ci"][i] = None, [None, None]
            y["complications"][i] = None if _small(c, threshold) else c
    y["stays"] = _suppress_series(y["stays"], threshold)

    a = v["by_age"]
    for i, (n, rate) in enumerate(zip(a["stays"], a["complication_rate"])):
        c = None if rate is None else round(rate * n)
        if _small(n, threshold) or _small(c, threshold):
            a["complication_rate"][i] = None
    a["stays"] = _suppress_series(a["stays"], threshold)

    v["by_type"]["count"] = _suppress_series(v["by_type"]["count"], threshold)
    for key in ("during", "after"):
        v["timing_by_type"][key] = _suppress_series(v["timing_by_type"][key], threshold)
    for r in v["rate_by_type"]:
        _hide_rate(r, "stays", threshold, ["rate", "ci"])
    v["los"]["stays"] = _suppress_series(v["los"]["stays"], threshold)
    v["readmit_days"]["stays"] = _suppress_series(v["readmit_days"]["stays"], threshold)

    ls = v["los_by_status"]
    for i, n in enumerate(ls["stays"]):
        if _small(n, threshold):
            ls["stays"][i], ls["avg_los"][i] = None, None

    for r in v["hip_vs_knee"]:
        if any(_small(r[k], threshold) for k in ("hip", "knee", "hip_n", "knee_n")):
            for k in ("hip", "knee", "hip_n", "knee_n"):
                r[k] = None if _small(r[k], threshold) else r[k]
            r["odds_ratio"], r["ci"] = None, [None, None]
            r["age_adjusted_odds_ratio"], r["age_adjusted_ci"] = None, [None, None]


def suppress(data, threshold=11):
    """Return a copy of the dashboard data with small counts hidden (see module docstring)."""
    out = copy.deepcopy(data)
    for key, view in out["views"].items():
        if key.startswith("__funnel__"):
            if _small(view["complications"], threshold) or _small(view["stays"], threshold):
                for k in view:
                    if k != "n":
                        view[k] = [None] * len(view["n"]) if isinstance(view[k], list) else None
        elif key.startswith("__hospitals__"):
            for row in view:
                _suppress_kpis(row, threshold)
        else:
            _suppress_view(view, threshold)
    out["suppressed_under"] = threshold
    return out
