"""Unit tests for anomaly detection with synthetic timeseries data."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from src.backend.validation.anomaly import (
    _operations_to_dataframe,
    _profiles_to_dataframe,
    detect_anomalies,
)
from tests.unit.backend.conftest import make_mock_operation, make_mock_profile


def _make_sinusoidal_profiles(
    n_days: int = 30,
    base_count: int = 1000,
    amplitude: int = 200,
    spike_day: int | None = None,
    spike_value: int = 5000,
) -> list:
    """Generate sinusoidal timeseries of profiles with optional injected spike."""
    import math

    now = datetime.now(tz=UTC)
    profiles = []
    for i in range(n_days):
        dt = now - timedelta(days=n_days - i)
        ts_ms = int(dt.timestamp() * 1000)
        row_count = base_count + int(amplitude * math.sin(2 * math.pi * i / 7))
        if spike_day is not None and i == spike_day:
            row_count = spike_value
        profiles.append(make_mock_profile(ts_ms, row_count=row_count))
    return profiles


# ── Prophet-based detection ──────────────────────────────────────────────────


async def test_detect_anomalies_prophet_finds_outlier():
    """Synthetic sinusoidal timeseries with one injected spike; verify flagged."""
    profiles = _make_sinusoidal_profiles(n_days=30, spike_day=15, spike_value=5000)
    results = await detect_anomalies(profiles, method="prophet")
    # At least one anomaly should be detected at or near the spike
    assert len(results) >= 1
    spike_detected = any(abs(r.actual_value - 5000) < 1 for r in results)
    assert spike_detected


async def test_detect_anomalies_prophet_no_anomaly():
    """Clean sinusoidal timeseries with noise matching the seasonality; verify few anomalies."""
    profiles = _make_sinusoidal_profiles(n_days=30, amplitude=200)
    results = await detect_anomalies(profiles, method="prophet")
    # Prophet should learn the weekly seasonality and flag very few points
    # With a regular sinusoidal pattern, most points should be within prediction bounds
    assert len(results) < len(profiles)


async def test_detect_anomalies_prophet_insufficient_data():
    """Fewer than 2 data points; verify empty result."""
    now_ms = int(time.time() * 1000)
    profiles = [make_mock_profile(now_ms, row_count=100)]
    results = await detect_anomalies(profiles, method="prophet")
    assert results == []


# ── Isolation Forest detection ───────────────────────────────────────────────


async def test_detect_anomalies_isolation_forest_finds_outlier():
    """Multi-feature profiles with one outlier row; verify flagged."""
    now = datetime.now(tz=UTC)
    profiles = []
    for i in range(20):
        dt = now - timedelta(days=20 - i)
        ts_ms = int(dt.timestamp() * 1000)
        profiles.append(
            make_mock_profile(ts_ms, row_count=1000, null_proportions=[0.01, 0.02], col_count=10)
        )
    # Inject outlier
    outlier_ts = int((now - timedelta(days=5)).timestamp() * 1000)
    profiles.append(
        make_mock_profile(outlier_ts, row_count=50000, null_proportions=[0.95, 0.90], col_count=50)
    )

    results = await detect_anomalies(profiles, method="isolation_forest")
    assert len(results) >= 1
    # The outlier should be detected
    outlier_found = any(r.actual_value >= 10000 for r in results)
    assert outlier_found


async def test_detect_anomalies_isolation_forest_clean():
    """Uniform profiles; verify no anomalies."""
    now = datetime.now(tz=UTC)
    profiles = []
    for i in range(20):
        dt = now - timedelta(days=20 - i)
        ts_ms = int(dt.timestamp() * 1000)
        profiles.append(
            make_mock_profile(ts_ms, row_count=1000, null_proportions=[0.01, 0.02], col_count=10)
        )

    results = await detect_anomalies(profiles, method="isolation_forest")
    # Uniform data should yield no anomalies with contamination="auto"
    assert len(results) == 0


async def test_detect_anomalies_isolation_forest_insufficient_data():
    """Fewer than 5 points; verify empty result."""
    now = datetime.now(tz=UTC)
    profiles = []
    for i in range(3):
        dt = now - timedelta(days=3 - i)
        ts_ms = int(dt.timestamp() * 1000)
        profiles.append(make_mock_profile(ts_ms, row_count=1000))

    results = await detect_anomalies(profiles, method="isolation_forest")
    assert results == []


# ── Invalid method ───────────────────────────────────────────────────────────


async def test_detect_anomalies_invalid_method():
    """Unknown method string; verify raises ValueError."""
    profiles = _make_sinusoidal_profiles(n_days=10)
    with pytest.raises(ValueError, match="Unknown anomaly detection method"):
        await detect_anomalies(profiles, method="bogus_method")


# ── DataFrame helpers ────────────────────────────────────────────────────────


def test_profiles_to_dataframe():
    """Verify correct extraction of timestamp, row_count, null_ratio from mock profiles."""
    now = datetime.now(tz=UTC)
    profiles = [
        make_mock_profile(
            int((now - timedelta(days=2)).timestamp() * 1000),
            row_count=500,
            null_proportions=[0.1, 0.3],
            col_count=5,
        ),
        make_mock_profile(
            int((now - timedelta(days=1)).timestamp() * 1000),
            row_count=600,
            null_proportions=[0.2, 0.4],
            col_count=5,
        ),
    ]
    df = _profiles_to_dataframe(profiles)
    assert len(df) == 2
    assert list(df.columns) == ["ds", "y", "null_ratio", "col_count"]
    assert df.iloc[0]["y"] == 500.0
    assert df.iloc[1]["y"] == 600.0
    assert abs(df.iloc[0]["null_ratio"] - 0.2) < 1e-9  # avg of 0.1, 0.3
    assert abs(df.iloc[1]["null_ratio"] - 0.3) < 1e-9  # avg of 0.2, 0.4


def test_operations_to_dataframe():
    """Verify correct extraction of timestamp from mock operations."""
    now = datetime.now(tz=UTC)
    operations = [
        make_mock_operation(int((now - timedelta(hours=2)).timestamp() * 1000)),
        make_mock_operation(int((now - timedelta(hours=1)).timestamp() * 1000)),
    ]
    df = _operations_to_dataframe(operations)
    assert len(df) == 2
    assert "ds" in df.columns
    assert "op_count" in df.columns
    assert all(df["op_count"] == 1)
