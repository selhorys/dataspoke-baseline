"""Anomaly detection on DataHub timeseries aspects (Prophet / Isolation Forest)."""

import asyncio
import logging
from datetime import UTC, datetime

from src.shared.models.quality import AnomalyResult

logger = logging.getLogger(__name__)

_MIN_PROPHET_POINTS = 2
_MIN_ISOLATION_FOREST_POINTS = 5


async def detect_anomalies(
    profiles: list,
    operations: list | None = None,
    method: str = "prophet",
) -> list[AnomalyResult]:
    """Detect anomalies in timeseries data from DataHub aspects.

    Args:
        profiles: List of DatasetProfileClass instances.
        operations: Optional list of OperationClass instances.
        method: Detection algorithm — "prophet" or "isolation_forest".

    Returns:
        List of AnomalyResult for each detected anomaly.
    """
    if method == "prophet":
        return await _detect_prophet(profiles, operations)
    elif method == "isolation_forest":
        return await _detect_isolation_forest(profiles)
    else:
        raise ValueError(f"Unknown anomaly detection method: {method!r}")


async def _detect_prophet(
    profiles: list,
    operations: list | None = None,
) -> list[AnomalyResult]:
    """Prophet-based seasonal anomaly detection on row counts."""
    ts = _profiles_to_dataframe(profiles)
    if len(ts) < _MIN_PROPHET_POINTS:
        return []

    try:
        from prophet import Prophet
    except ImportError:
        logger.warning("prophet not installed; skipping Prophet-based anomaly detection")
        return []

    def _fit_and_predict():

        # Prophet requires timezone-naive timestamps
        fit_df = ts[["ds", "y"]].copy()
        fit_df["ds"] = fit_df["ds"].dt.tz_localize(None)
        m = Prophet(interval_width=0.95)
        m.fit(fit_df)
        forecast = m.predict(fit_df[["ds"]])
        return forecast

    forecast = await asyncio.to_thread(_fit_and_predict)

    results: list[AnomalyResult] = []
    for i, row in forecast.iterrows():
        actual = ts.iloc[i]["y"]
        yhat = row["yhat"]
        upper = row["yhat_upper"]
        lower = row["yhat_lower"]

        residual = abs(actual - yhat)
        range_width = upper - lower

        # Skip if uncertainty interval is near-zero (perfect fit, no real anomaly)
        if range_width < 1e-6:
            continue

        if actual > upper or actual < lower:
            confidence = min(residual / range_width, 1.0) if range_width > 0 else 0.5

            ts_val = ts.iloc[i]["ds"]
            detected_at = ts_val.to_pydatetime() if hasattr(ts_val, "to_pydatetime") else ts_val
            if detected_at.tzinfo is None:
                detected_at = detected_at.replace(tzinfo=UTC)

            results.append(
                AnomalyResult(
                    metric_name="row_count",
                    is_anomaly=True,
                    expected_value=round(yhat, 2),
                    actual_value=round(actual, 2),
                    confidence=round(confidence, 4),
                    detected_at=detected_at,
                )
            )

    return results


async def _detect_isolation_forest(profiles: list) -> list[AnomalyResult]:
    """Isolation Forest anomaly detection on profile features."""
    ts = _profiles_to_dataframe(profiles)
    if len(ts) < _MIN_ISOLATION_FOREST_POINTS:
        return []

    try:
        from sklearn.ensemble import IsolationForest
    except ImportError:
        logger.warning("scikit-learn not installed; skipping Isolation Forest anomaly detection")
        return []

    import numpy as np

    feature_cols = [c for c in ts.columns if c not in ("ds",)]
    X = ts[feature_cols].values

    # Skip if all features have near-zero variance (identical data)
    if np.all(np.std(X, axis=0) < 1e-10):
        return []

    def _fit_and_predict():
        clf = IsolationForest(contamination="auto", random_state=42)
        labels = clf.fit_predict(X)
        scores = clf.decision_function(X)
        return labels, scores

    labels, scores = await asyncio.to_thread(_fit_and_predict)

    results: list[AnomalyResult] = []
    for i, (label, score_val) in enumerate(zip(labels, scores)):
        if label == -1:
            ts_val = ts.iloc[i]["ds"]
            detected_at = ts_val.to_pydatetime() if hasattr(ts_val, "to_pydatetime") else ts_val
            if detected_at.tzinfo is None:
                detected_at = detected_at.replace(tzinfo=UTC)

            confidence = max(0.0, min(1.0, -score_val))

            results.append(
                AnomalyResult(
                    metric_name="row_count",
                    is_anomaly=True,
                    expected_value=round(float(ts["y"].median()), 2),
                    actual_value=round(float(ts.iloc[i]["y"]), 2),
                    confidence=round(confidence, 4),
                    detected_at=detected_at,
                )
            )

    return results


def _profiles_to_dataframe(profiles: list):
    """Convert DatasetProfileClass instances to a pandas DataFrame."""
    import pandas as pd

    rows = []
    for p in profiles:
        ts_ms = getattr(p, "timestampMillis", None)
        if ts_ms is None:
            continue
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        row_count = getattr(p, "rowCount", None) or 0

        # Compute average null ratio from field profiles
        null_ratio = 0.0
        field_profiles = getattr(p, "fieldProfiles", None) or []
        if field_profiles:
            null_vals = [
                fp.nullProportion
                for fp in field_profiles
                if hasattr(fp, "nullProportion") and fp.nullProportion is not None
            ]
            if null_vals:
                null_ratio = sum(null_vals) / len(null_vals)

        col_count = getattr(p, "columnCount", None) or 0

        rows.append(
            {
                "ds": dt,
                "y": float(row_count),
                "null_ratio": null_ratio,
                "col_count": float(col_count),
            }
        )

    if not rows:
        return pd.DataFrame(columns=["ds", "y", "null_ratio", "col_count"])

    df = pd.DataFrame(rows)
    df = df.sort_values("ds").reset_index(drop=True)
    return df


def _operations_to_dataframe(operations: list):
    """Convert OperationClass instances to a pandas DataFrame."""
    import pandas as pd

    rows = []
    for op in operations:
        ts_ms = getattr(op, "lastUpdatedTimestamp", None) or getattr(op, "timestampMillis", None)
        if ts_ms is None:
            continue
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        rows.append({"ds": dt, "op_count": 1})

    if not rows:
        return pd.DataFrame(columns=["ds", "op_count"])

    df = pd.DataFrame(rows)
    df = df.sort_values("ds").reset_index(drop=True)
    return df
