"""Cost forecasting: given a set of (x, cost) points -- e.g. video length in minutes vs. translation
cost -- fit several candidate REGRESSION shapes (linear, sqrt, quadratic, power) and report which one
actually fits best (by R-squared on the real cost units, not a log-transformed proxy), rather than
assuming a shape up front. Separately, fit several candidate PROBABILITY DISTRIBUTIONS to a set of cost
values (e.g. normal, gamma, lognormal, exponential, pareto) and report which fits best by AIC, for
understanding the spread/uncertainty around a forecast, not just its central estimate.

Needs `numpy`/`scipy` -- install via `pip install django-llm-ledger[forecasting]`. Importing this
module without them raises ImportError with that exact instruction, not a bare ModuleNotFoundError,
since this is the one part of the package that isn't a core dependency."""

from __future__ import annotations

try:
    import numpy as np
    from scipy import stats
except ImportError as exc:  # pragma: no cover - exercised only when the extra isn't installed
    raise ImportError(
        "llm_ledger.forecasting needs numpy and scipy -- install with "
        "`pip install django-llm-ledger[forecasting]`"
    ) from exc


class NotEnoughData(ValueError):
    """Raised instead of letting numpy/scipy fail with an opaque linear-algebra error on too few
    points -- a quadratic fit needs at least 3 points to even be defined, for instance."""


def _r_squared(y: "np.ndarray", predicted: "np.ndarray") -> float:
    ss_res = float(np.sum((y - predicted) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0


def _fit_linear(x: "np.ndarray", y: "np.ndarray") -> dict:
    a, b = np.polyfit(x, y, 1)
    predicted = a * x + b
    return {"form": "linear", "formula": "y = a*x + b", "params": {"a": float(a), "b": float(b)}, "r_squared": _r_squared(y, predicted)}


def _fit_sqrt(x: "np.ndarray", y: "np.ndarray") -> dict | None:
    if (x < 0).any():
        return None  # sqrt of a negative x is undefined -- this form just doesn't apply to this data
    sx = np.sqrt(x)
    a, b = np.polyfit(sx, y, 1)
    predicted = a * sx + b
    return {"form": "sqrt", "formula": "y = a*sqrt(x) + b", "params": {"a": float(a), "b": float(b)}, "r_squared": _r_squared(y, predicted)}


def _fit_quadratic(x: "np.ndarray", y: "np.ndarray") -> dict | None:
    if len(x) < 3:
        return None  # a degree-2 fit is underdetermined (or exactly-determined/overfit) below 3 points
    a, b, c = np.polyfit(x, y, 2)
    predicted = a * x**2 + b * x + c
    return {"form": "quadratic", "formula": "y = a*x^2 + b*x + c", "params": {"a": float(a), "b": float(b), "c": float(c)}, "r_squared": _r_squared(y, predicted)}


def _fit_power(x: "np.ndarray", y: "np.ndarray") -> dict | None:
    if (x <= 0).any() or (y <= 0).any():
        return None  # log(x)/log(y) undefined for non-positive values -- this form doesn't apply
    b, log_a = np.polyfit(np.log(x), np.log(y), 1)
    a = float(np.exp(log_a))
    predicted = a * x**b
    return {"form": "power", "formula": "y = a*x^b", "params": {"a": a, "b": float(b)}, "r_squared": _r_squared(y, predicted)}


REGRESSION_FITTERS = {"linear": _fit_linear, "sqrt": _fit_sqrt, "quadratic": _fit_quadratic, "power": _fit_power}


def fit_best_regression(xs: list[float], ys: list[float]) -> dict:
    """Fits every applicable form in REGRESSION_FITTERS and returns {"candidates": [...], "best":
    "<form name>"} -- "best" is whichever has the highest R-squared, computed on the actual cost
    values so every form is compared on the same footing (a form that only fits well after a log
    transform, for instance, isn't silently favored). A form that doesn't apply to this data (e.g.
    sqrt with a negative x) is simply omitted from "candidates", not reported as a failure."""
    if len(xs) < 2:
        raise NotEnoughData(f"need at least 2 points to fit a regression, got {len(xs)}")
    x, y = np.array(xs, dtype=float), np.array(ys, dtype=float)
    candidates = [c for c in (fitter(x, y) for fitter in REGRESSION_FITTERS.values()) if c is not None]
    if not candidates:
        raise NotEnoughData("no regression form applies to this data (check for negative/zero x or y)")
    best = max(candidates, key=lambda c: c["r_squared"])
    return {"candidates": candidates, "best": best["form"]}


def predict_from_fit(fit_result: dict, x_new: float) -> float:
    """Applies whichever candidate `fit_result["best"]` names to a new x -- call this with the output
    of `fit_best_regression` directly."""
    best = next(c for c in fit_result["candidates"] if c["form"] == fit_result["best"])
    p = best["params"]
    if best["form"] == "linear":
        return p["a"] * x_new + p["b"]
    if best["form"] == "sqrt":
        return p["a"] * (x_new**0.5) + p["b"]
    if best["form"] == "quadratic":
        return p["a"] * x_new**2 + p["b"] * x_new + p["c"]
    if best["form"] == "power":
        return p["a"] * x_new ** p["b"]
    raise ValueError(f"unknown regression form {best['form']!r}")  # pragma: no cover - REGRESSION_FITTERS is exhaustive


DISTRIBUTIONS = {
    "norm": stats.norm,
    "lognorm": stats.lognorm,
    "gamma": stats.gamma,
    "expon": stats.expon,
    "pareto": stats.pareto,
}


def fit_best_distribution(values: list[float]) -> dict:
    """Fits every distribution in DISTRIBUTIONS by maximum likelihood and returns {"candidates": [...],
    "best": "<name>"} -- "best" is whichever has the lowest AIC (penalizes extra parameters, so a
    3-parameter distribution has to earn a real improvement in fit over a 2-parameter one, not just
    exploit having more knobs). Each candidate also reports a Kolmogorov-Smirnov statistic/p-value as a
    second, independent goodness-of-fit signal."""
    if len(values) < 5:
        raise NotEnoughData(f"need at least 5 values for a meaningful distribution fit, got {len(values)}")
    y = np.array(values, dtype=float)
    candidates = []
    for name, dist in DISTRIBUTIONS.items():
        try:
            params = dist.fit(y)
            log_likelihood = float(np.sum(dist.logpdf(y, *params)))
            if not np.isfinite(log_likelihood):
                continue
            aic = 2 * len(params) - 2 * log_likelihood
            ks_stat, ks_pvalue = stats.kstest(y, dist.cdf, args=params)
            candidates.append(
                {
                    "distribution": name,
                    "params": [float(p) for p in params],
                    "log_likelihood": log_likelihood,
                    "aic": float(aic),
                    "ks_statistic": float(ks_stat),
                    "ks_pvalue": float(ks_pvalue),
                }
            )
        except Exception:
            continue  # a distribution that can't be fit to this data (e.g. pareto needs positive support) is skipped, not fatal
    if not candidates:
        raise NotEnoughData("no distribution in DISTRIBUTIONS could be fit to this data")
    best = min(candidates, key=lambda c: c["aic"])
    return {"candidates": candidates, "best": best["distribution"]}
