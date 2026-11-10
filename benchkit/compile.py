"""Compile a bench suite into a bundle the way a host would: the suite's schema, its term
catalogue, and its gold queries as the host's verified references.

``python -m benchkit compile <suite> [--engine duckdb] [--seed N] [--profiles a,b,c]``
writes ``bench/.runs/bundles/<suite>-<engine>/``. ``evaluate --bundle`` then scores the
candidates with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlgami import Catalog
from sqlgami.bundle import Bundle, compile_bundle

from .suite import RUNS_DIR, Suite


def bundle_dir(suite: Suite, engine: str) -> Path:
    return RUNS_DIR / "bundles" / f"{suite.path.name}-{engine}"


def compile_suite(
    suite: Suite,
    engine: str = "duckdb",
    seed: int = 1,
    profiles: list[str] | None = None,
    out: Path | None = None,
    from_seeds: bool = False,
    rules_file: Path | None = None,
) -> Bundle:
    """``from_seeds`` builds one fixture per nasty seed by subsampling the suite's own databases
    with the ``subsample:`` predicates of ``rules_file`` (what the fixture study did), instead of
    generating synthetic rows."""
    ddl = suite.schema_file(engine).read_text(encoding="utf-8")
    config_block = suite.config.get("sqlgami") or {}
    catalog = Catalog.from_ddl(ddl, engine, snapshot_columns=config_block.get("snapshot_columns") or None)
    rules: dict[str, Any] = dict(config_block.get("fixtures") or {})
    rules.setdefault("seed", seed)
    if profiles:
        rules["profiles"] = profiles
    # The questions' parameter values are what the fixtures are built around.
    params: dict[str, list[Any]] = dict(rules.get("params") or {})
    for question in suite.questions:
        for name, value in (question.param_values() or {}).items():
            values = params.setdefault(name, [])
            if value not in values:
                values.append(value)
    rules["params"] = params
    references = [
        {"key": q.id, "question": q.question, "sql": q.gold(engine), "params": q.param_values() or {}}
        for q in suite.questions
        if q.gold(engine)
    ]
    config: dict[str, Any] = {
        "dialect": engine,
        "snapshot_columns": config_block.get("snapshot_columns"),
        "required_predicates": config_block.get("required_predicates"),
        "fixtures": rules,
        "references": references,
    }
    terms = suite.terms_source(engine)
    if terms is not None:
        config["terms"] = str(terms)
    prebuilt = None
    if from_seeds:
        import yaml

        from sqlgami.fixtures import from_connection

        from . import build

        extra = yaml.safe_load(rules_file.read_text(encoding="utf-8")) if rules_file else {}
        where = (extra or {}).get("subsample") or {}
        prebuilt = []
        for s in suite.nasty_seeds() or suite.seeds:
            conn = build.connect(suite, engine, s)
            try:
                prebuilt.append(from_connection(conn, catalog, f"seed-{s.seed}", where=where, profile="subsample", seed=s.seed))
            finally:
                conn.close()
    return compile_bundle(config, out or bundle_dir(suite, engine), catalog=catalog, fixtures=prebuilt)
