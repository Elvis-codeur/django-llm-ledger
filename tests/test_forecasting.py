"""forecasting.py: best-fit regression selection (linear/sqrt/quadratic/power) and best-fit
distribution selection (normal/lognormal/gamma/exponential/pareto), both via real goodness-of-fit
metrics computed on real data, not assumed."""

import numpy as np
import pytest

from llm_ledger.forecasting import NotEnoughData, fit_best_distribution, fit_best_regression, predict_from_fit


def test_fit_best_regression_recovers_a_known_sqrt_relationship():
    # y = 3*sqrt(x) + 1, exactly -- the regression should recognize "sqrt" as the clear winner.
    xs = [1, 4, 9, 16, 25, 36, 49, 64]
    ys = [3 * (x**0.5) + 1 for x in xs]
    result = fit_best_regression(xs, ys)
    assert result["best"] == "sqrt"
    sqrt_candidate = next(c for c in result["candidates"] if c["form"] == "sqrt")
    assert sqrt_candidate["r_squared"] > 0.999
    assert sqrt_candidate["params"]["a"] == pytest.approx(3.0, abs=1e-3)
    assert sqrt_candidate["params"]["b"] == pytest.approx(1.0, abs=1e-2)


def test_fit_best_regression_recovers_a_known_quadratic_relationship():
    xs = list(range(1, 10))
    ys = [2 * x**2 + 3 * x + 5 for x in xs]
    result = fit_best_regression(xs, ys)
    assert result["best"] == "quadratic"


def test_fit_best_regression_recovers_a_known_power_law():
    xs = [1, 2, 4, 8, 16, 32]
    ys = [5 * (x**1.8) for x in xs]
    result = fit_best_regression(xs, ys)
    assert result["best"] == "power"
    power_candidate = next(c for c in result["candidates"] if c["form"] == "power")
    assert power_candidate["params"]["b"] == pytest.approx(1.8, abs=1e-2)


def test_predict_from_fit_applies_the_winning_forms_params():
    xs = [1, 4, 9, 16, 25]
    ys = [2 * (x**0.5) for x in xs]  # y = 2*sqrt(x), no intercept noise
    result = fit_best_regression(xs, ys)
    assert result["best"] == "sqrt"
    assert predict_from_fit(result, 100) == pytest.approx(20.0, abs=0.1)  # 2*sqrt(100) = 20


def test_sqrt_and_power_forms_are_omitted_not_errored_for_negative_x():
    xs = [-2, -1, 0, 1, 2]
    ys = [4, 1, 0, 1, 4]  # a perfect parabola, y = x^2
    result = fit_best_regression(xs, ys)
    forms = {c["form"] for c in result["candidates"]}
    assert "sqrt" not in forms
    assert "power" not in forms
    assert result["best"] == "quadratic"


def test_fit_best_regression_raises_on_too_few_points():
    with pytest.raises(NotEnoughData):
        fit_best_regression([1], [1])


def test_fit_best_distribution_recovers_a_normal_distribution():
    rng = np.random.default_rng(42)
    values = rng.normal(loc=10.0, scale=2.0, size=500).tolist()
    result = fit_best_distribution(values)
    assert result["best"] == "norm"


def test_fit_best_distribution_recovers_a_gamma_distribution():
    rng = np.random.default_rng(7)
    values = rng.gamma(shape=2.0, scale=1.5, size=800).tolist()
    result = fit_best_distribution(values)
    # gamma-distributed data is often also plausibly lognormal at these parameters (both are positive,
    # right-skewed) -- the real assertion is that it picked a right-skewed positive-support shape, not
    # the symmetric normal, which is the actual mistake this feature exists to avoid.
    assert result["best"] in {"gamma", "lognorm"}


def test_fit_best_distribution_raises_on_too_few_values():
    with pytest.raises(NotEnoughData):
        fit_best_distribution([1.0, 2.0, 3.0])


def test_fit_best_distribution_reports_all_candidates_with_aic():
    rng = np.random.default_rng(1)
    values = rng.normal(loc=5.0, scale=1.0, size=200).tolist()
    result = fit_best_distribution(values)
    names = {c["distribution"] for c in result["candidates"]}
    assert "norm" in names
    for candidate in result["candidates"]:
        assert "aic" in candidate and "ks_pvalue" in candidate
