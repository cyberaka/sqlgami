"""Latency of ``score()`` on a suite's statements: p50 / p90 / p99 per mode.

``python -m benchkit latency <suite> [--engine duckdb] [--mode static] [--mode sample]
[--repeat 3] [--stretch 40-80]``. ``--stretch`` pads each statement to the requested line
range by wrapping it in pass-through CTEs and extra predicates, which is how the owner's
40-80-line production SQL looks to the parser without changing the answer.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from sqlgami import Scorer

from . import build
from .suite import Suite


def percentiles(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {}
    ordered = sorted(samples)
    n = len(ordered)

    def at(p: float) -> float:
        return ordered[min(n - 1, int(round(p * (n - 1))))]

    return {"n": n, "p50": at(0.5), "p90": at(0.9), "p99": at(0.99), "max": ordered[-1], "mean": statistics.fmean(ordered)}


def stretch(sql: str, lines: tuple[int, int]) -> str:
    """Pad a statement to ``lines`` (low, high) lines with up to three unused CTEs of many
    projection lines, so the parser, the resolver and the checks see production-sized text with
    a production-like number of scopes, and the result is unchanged."""
    low, high = lines
    current = sql.count("\n") + 1
    if current >= low:
        return sql
    target = (low + high) // 2
    body = sql.rstrip().rstrip(";")
    extra = max(1, target - current - 2)
    ctes = min(3, max(1, extra // 12))
    per = max(1, extra // ctes)
    filler = ",\n".join(
        f"  sqlgami_pad_{c} AS (SELECT\n" + ",\n".join(f"    {c * 100 + i} AS pad_{i}" for i in range(per)) + ")"
        for c in range(ctes)
    )
    if body.lstrip().upper().startswith("WITH "):
        return f"WITH {filler.lstrip()},\n{body.lstrip()[5:]}"
    return f"WITH {filler.lstrip()}\n{body}"


def measure(
    suite: Suite,
    engine: str,
    modes: list[str],
    repeat: int = 3,
    lines: tuple[int, int] | None = None,
) -> dict[str, Any]:
    ddl = suite.schema_file(engine).read_text(encoding="utf-8")
    config = suite.scorer_config(engine)
    config.pop("tenant_column", None)
    glossary_file = suite.path / "glossary.yaml"
    glossary = glossary_file.read_text(encoding="utf-8") if glossary_file.exists() else None
    terms = suite.terms_source(engine)
    kwargs: dict[str, Any] = dict(glossary=glossary, terms=str(terms) if terms else None, **config)
    statements = [(q.gold(engine), q.question, q.param_values() or None) for q in suite.questions if q.gold(engine)]
    if lines:
        statements = [(stretch(sql, lines), question, params) for sql, question, params in statements]
    result: dict[str, Any] = {"suite": suite.name, "engine": engine, "statements": len(statements), "lines": None, "modes": {}}
    if statements:
        counts = [s.count("\n") + 1 for s, _, _ in statements]
        result["lines"] = {"p50": sorted(counts)[len(counts) // 2], "min": min(counts), "max": max(counts)}
    for mode in modes:
        if mode == "static":
            scorer = Scorer(engine, catalog=ddl, **kwargs)
        else:
            seed = (suite.nasty_seeds() or suite.seeds)[-1]
            scorer = Scorer(engine, db=build.connect(suite, engine, seed), **kwargs)
        method = {"static": scorer.explain, "dry": scorer.dry_run, "sample": scorer.sample}[mode]
        for sql, question, params in statements[:3]:  # warm the caches, as a long-running host would be
            method(sql, question, params=params)
        samples: list[float] = []
        for _ in range(repeat):
            for sql, question, params in statements:
                started = time.perf_counter()
                method(sql, question, params=params)
                samples.append((time.perf_counter() - started) * 1000)
        scorer.close()
        result["modes"][mode] = percentiles(samples)
    return result


def format_result(result: dict[str, Any]) -> str:
    lines = [f"latency: {result['suite']} on {result['engine']}, {result['statements']} statements"
             + (f", {result['lines']['p50']} lines median ({result['lines']['min']}-{result['lines']['max']})" if result.get("lines") else "")]
    for mode, stats in result["modes"].items():
        if stats:
            lines.append(f"  {mode:7} p50 {stats['p50']:6.1f} ms  p90 {stats['p90']:6.1f} ms  p99 {stats['p99']:6.1f} ms  max {stats['max']:6.1f} ms  (n={stats['n']})")
    return "\n".join(lines)


def write_result(result: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            existing = []
    existing.append({**result, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    path.write_text(json.dumps(existing[-50:], indent=1), encoding="utf-8")
