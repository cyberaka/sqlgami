"""Turn provisional scores into measured probabilities.

A calibrator is fitted on statements whose correctness is known. It learns, for
each depth of checking (static, dry, sample), what share of statements with a
given provisional score were in fact correct, and maps new scores accordingly.
With a calibrator loaded, "0.8" means that about 80% of comparable statements
in the fitting data were correct. That holds only as far as new workloads
resemble the fitting data, which is why reports stay advisory.

The method is isotonic regression (pool adjacent violators): it assumes only
that a higher provisional score should not mean a lower chance of being right.
Standard library only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

FORMAT_VERSION = 1
DEPTHS = ("static", "dry", "sample")


@dataclass
class Curve:
    """A non-decreasing step function from provisional score to share correct."""

    xs: list[float] = field(default_factory=list)  # mean provisional score of each block
    ys: list[float] = field(default_factory=list)  # smoothed share correct in each block
    counts: list[int] = field(default_factory=list)

    def at(self, x: float) -> float:
        xs, ys = self.xs, self.ys
        if not xs:
            raise ValueError("empty curve")
        if x <= xs[0]:
            return ys[0]
        if x >= xs[-1]:
            return ys[-1]
        for i in range(1, len(xs)):
            if x <= xs[i]:
                span = xs[i] - xs[i - 1]
                if span <= 0:
                    return ys[i]
                weight = (x - xs[i - 1]) / span
                return ys[i - 1] + weight * (ys[i] - ys[i - 1])
        return ys[-1]


def fit_curve(points: Iterable[tuple[float, bool]]) -> Curve:
    """Isotonic fit of correctness against score, with add-one smoothing per block."""
    # One starting block per distinct score, so ties are never split.
    by_score: dict[float, list[float]] = {}
    for x, y in points:
        entry = by_score.setdefault(float(x), [0.0, 0])
        entry[0] += 1.0 if y else 0.0
        entry[1] += 1
    blocks: list[list[float]] = []  # [sum_x, sum_y, n]
    for x in sorted(by_score):
        sum_y, n = by_score[x]
        blocks.append([x * n, sum_y, n])
        # Pool adjacent violators: a higher score must not have a lower share correct.
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][2] > blocks[-1][1] / blocks[-1][2]:
            last = blocks.pop()
            blocks[-1][0] += last[0]
            blocks[-1][1] += last[1]
            blocks[-1][2] += last[2]
    curve = Curve()
    for sum_x, sum_y, n in blocks:
        curve.xs.append(sum_x / n)
        curve.ys.append((sum_y + 1) / (n + 2))  # never exactly 0 or 1 on finite evidence
        curve.counts.append(int(n))
    # Smoothing can break monotonicity between a small block and a large one; restore it.
    for i in range(1, len(curve.ys)):
        if curve.ys[i] < curve.ys[i - 1]:
            curve.ys[i] = curve.ys[i - 1]
    return curve


class IsotonicCalibrator:
    """Per-depth isotonic calibration. Satisfies ``sqlgami.scoring.Calibrator``."""

    def __init__(self, curves: dict[str, Curve] | None = None, meta: dict[str, Any] | None = None) -> None:
        self.curves: dict[str, Curve] = curves or {}
        self.meta: dict[str, Any] = meta or {}

    @classmethod
    def fit(
        cls,
        samples: Iterable[tuple[float, bool, str]],
        min_samples: int = 30,
        meta: dict[str, Any] | None = None,
    ) -> "IsotonicCalibrator":
        """Fit from ``(provisional score, correct, depth)`` triples.

        A depth with fewer than ``min_samples`` examples, or with only correct or
        only wrong examples, is left unfitted: scores at that depth stay provisional.
        """
        by_depth: dict[str, list[tuple[float, bool]]] = {}
        for score, correct, depth in samples:
            if score is None:
                continue
            by_depth.setdefault(depth, []).append((float(score), bool(correct)))
        curves: dict[str, Curve] = {}
        fitted_on: dict[str, int] = {}
        for depth, points in by_depth.items():
            labels = {y for _, y in points}
            if len(points) < min_samples or len(labels) < 2:
                continue
            curves[depth] = fit_curve(points)
            fitted_on[depth] = len(points)
        info = dict(meta or {})
        info["samples"] = fitted_on
        return cls(curves, info)

    def calibrate(self, raw: float, depth: str) -> float | None:
        curve = self.curves.get(depth)
        if curve is None or not curve.xs:
            return None
        return min(1.0, max(0.0, curve.at(float(raw))))

    # ------------------------------------------------------- persistence

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT_VERSION,
            "method": "isotonic",
            "meta": self.meta,
            "curves": {
                depth: {"xs": c.xs, "ys": c.ys, "counts": c.counts} for depth, c in self.curves.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IsotonicCalibrator":
        if not isinstance(data, dict) or data.get("method") != "isotonic":
            raise ValueError("not an isotonic calibration document")
        if data.get("format") != FORMAT_VERSION:
            raise ValueError(f"unsupported calibration format {data.get('format')!r}")
        curves = {}
        for depth, raw in (data.get("curves") or {}).items():
            xs, ys = list(raw["xs"]), list(raw["ys"])
            if len(xs) != len(ys) or not xs:
                raise ValueError(f"malformed curve for depth {depth!r}")
            curves[depth] = Curve(xs, ys, list(raw.get("counts") or [0] * len(xs)))
        return cls(curves, dict(data.get("meta") or {}))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "IsotonicCalibrator":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            from .errors import ConfigurationError

            raise ConfigurationError(f"could not read calibration file {path}: {exc}") from exc
        try:
            return cls.from_dict(data)
        except (ValueError, KeyError, TypeError) as exc:
            from .errors import ConfigurationError

            raise ConfigurationError(f"calibration file {path} is not usable: {exc}") from exc
