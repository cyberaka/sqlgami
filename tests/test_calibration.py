"""Isotonic calibration: fitting, monotonicity, persistence and use by the scorer."""

from __future__ import annotations

import json
import random

import pytest

from sqlgami import Catalog, ConfigurationError, IsotonicCalibrator, Scorer
from sqlgami.calibration import fit_curve

from conftest import SHOP_DDL


def synthetic(n: int, seed: int = 7) -> list[tuple[float, bool, str]]:
    """Provisional scores that are overconfident: true chance of being right is score squared."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        score = rng.choice([0.2, 0.3, 0.4, 0.5, 0.6, 0.68, 0.76])
        out.append((score, rng.random() < score**2, "sample"))
    return out


def test_curve_is_monotone_and_tracks_the_data():
    curve = fit_curve([(s, y) for s, y, _ in synthetic(4000)])
    assert curve.ys == sorted(curve.ys)
    assert all(0 < y < 1 for y in curve.ys)
    assert curve.at(0.76) == pytest.approx(0.76**2, abs=0.05)
    assert curve.at(0.2) == pytest.approx(0.2**2, abs=0.05)
    assert curve.at(0.0) == curve.ys[0] and curve.at(1.0) == curve.ys[-1]
    assert curve.ys[0] <= curve.at(0.55) <= curve.ys[-1]


def test_violations_are_pooled():
    # Higher scores doing worse than lower ones must be merged, not preserved.
    points = [(0.3, True)] * 8 + [(0.3, False)] * 2 + [(0.6, True)] * 2 + [(0.6, False)] * 8
    curve = fit_curve(points)
    assert len(curve.xs) == 1 and curve.counts == [20]
    assert curve.ys[0] == pytest.approx(11 / 22)


def test_unfittable_depths_stay_provisional():
    calibrator = IsotonicCalibrator.fit(
        synthetic(500) + [(0.6, True, "dry")] * 10 + [(0.6, True, "static")] * 100
    )
    assert sorted(calibrator.curves) == ["sample"]
    assert calibrator.calibrate(0.6, "dry") is None  # too few examples
    assert calibrator.calibrate(0.6, "static") is None  # only one label seen
    assert 0 < calibrator.calibrate(0.6, "sample") < 1
    assert calibrator.meta["samples"] == {"sample": 500}


def test_round_trip_and_bad_files(tmp_path):
    calibrator = IsotonicCalibrator.fit(synthetic(800), meta={"suites": ["x"]})
    path = tmp_path / "cal.json"
    calibrator.save(path)
    loaded = IsotonicCalibrator.load(path)
    for score in (0.1, 0.33, 0.6, 0.76, 0.99):
        assert loaded.calibrate(score, "sample") == calibrator.calibrate(score, "sample")
    assert loaded.meta["suites"] == ["x"]
    json.loads(path.read_text())
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigurationError):
        IsotonicCalibrator.load(bad)
    bad.write_text(json.dumps({"method": "isotonic", "format": 999, "curves": {}}))
    with pytest.raises(ConfigurationError):
        IsotonicCalibrator.load(bad)
    with pytest.raises(ConfigurationError):
        IsotonicCalibrator.load(tmp_path / "missing.json")


def test_scorer_uses_a_calibrator_and_keeps_the_gates(tmp_path):
    static = [(s, y, "static") for s, y, _ in synthetic(800)]
    path = tmp_path / "cal.json"
    IsotonicCalibrator.fit(static).save(path)
    catalog = Catalog.from_ddl(SHOP_DDL, "duckdb")
    plain = Scorer("duckdb", catalog=catalog)
    calibrated = Scorer("duckdb", catalog=catalog, calibrator=str(path))

    sql = "SELECT name FROM customers ORDER BY name"
    before, after = plain.explain(sql), calibrated.explain(sql)
    assert before.confidence.calibrated is False and before.confidence.raw == before.score
    assert after.confidence.calibrated is True
    assert after.confidence.raw == pytest.approx(before.score)
    assert after.score < before.score  # the synthetic data says these scores were overconfident
    assert "calibrated on labelled data" in after.confidence.basis
    assert "raw" in after.to_dict()["confidence"]

    # Hard gates and caps still win over whatever the calibrator says.
    generous = IsotonicCalibrator.fit([(0.1, True, "static"), (0.9, True, "static")] * 20 + [(0.05, False, "static")] * 5)
    capped = Scorer("duckdb", catalog=catalog, calibrator=generous)
    high_finding = capped.explain("SELECT * FROM customers WHERE deleted_at = NULL")
    assert high_finding.score <= 0.4 and high_finding.confidence.high <= 0.4
    invalid = capped.explain("SELECT nope FROM customers")
    assert invalid.score <= 0.1 and invalid.confidence.calibrated is False
