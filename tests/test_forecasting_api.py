"""The /forecast-regression/ and /forecast-distribution/ HTTP endpoints -- thin wrappers around
forecasting.py, tested through the real DRF views (the path an SDK caller actually hits)."""

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def test_forecast_regression_picks_sqrt_and_predicts(client):
    points = [{"x": x, "y": 3 * (x**0.5) + 1} for x in [1, 4, 9, 16, 25, 36, 49, 64]]
    resp = client.post("/api/forecast-regression/", {"points": points, "predict_x": 100}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["best"] == "sqrt"
    assert resp.data["prediction"]["using"] == "sqrt"
    assert resp.data["prediction"]["y"] == pytest.approx(31.0, abs=0.5)  # 3*sqrt(100)+1 = 31


def test_forecast_regression_rejects_malformed_points(client):
    resp = client.post("/api/forecast-regression/", {"points": [{"x": "not-a-number"}]}, format="json")
    assert resp.status_code == 400


def test_forecast_regression_rejects_too_few_points(client):
    resp = client.post("/api/forecast-regression/", {"points": [{"x": 1, "y": 1}]}, format="json")
    assert resp.status_code == 400


def test_forecast_distribution_picks_normal_for_normal_data(client):
    import numpy as np

    rng = np.random.default_rng(3)
    values = rng.normal(loc=20.0, scale=3.0, size=300).tolist()
    resp = client.post("/api/forecast-distribution/", {"values": values}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["best"] == "norm"
    assert all("aic" in c for c in resp.data["candidates"])


def test_forecast_distribution_rejects_too_few_values(client):
    resp = client.post("/api/forecast-distribution/", {"values": [1.0, 2.0]}, format="json")
    assert resp.status_code == 400
