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
    references_file: Path | None = None,
    expand: str | None = None,
    per_term: int = 6,
    per_example: int = 4,
    accept: bool = False,
) -> Bundle:
    """``from_seeds`` builds one fixture per nasty seed by subsampling the suite's own databases
    with the ``subsample:`` predicates of ``rules_file`` (what the fixture study did), instead of
    generating synthetic rows. ``expand`` names a model (``hf:<name>``) for the offline expansion
    step; ``accept`` then writes the accepted wordings into the bundle's own copy of the Terms
    sheet (as a host would, after review), so an evaluation with ``--terms-from-bundle`` measures
    the expansion — a tuned number, since the model saw the suite's questions."""
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
    if references_file is not None:
        # Host-registered references (templated golds) replace the suite's gold of the same id.
        import json

        override = {}
        for line in references_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                override[row.get("id") or row.get("key")] = row
        kept = []
        for ref in references:
            row = override.pop(ref["key"], None)
            if row is None:
                kept.append(ref)
                continue
            question = suite.question(ref["key"])
            params = dict(ref["params"])
            kept.append({"key": ref["key"], "question": row["question"], "sql": row["sql"], "params": params, "n_default": row.get("n_default"), "slots": row.get("slots")})
        references = kept + [{"key": k, **{kk: vv for kk, vv in v.items() if kk != "id"}} for k, v in override.items()]
    config: dict[str, Any] = {
        "dialect": engine,
        "snapshot_columns": config_block.get("snapshot_columns"),
        "required_predicates": config_block.get("required_predicates"),
        "allowed_functions": config_block.get("allowed_functions") or [],
        "fixtures": rules,
        "references": references,
    }
    terms = suite.terms_source(engine)
    if terms is not None:
        config["terms"] = str(terms)
    if expand:
        slot_values: dict[str, list[Any]] = {}
        for question in suite.questions:  # the numbers the suite's questions use, as values to vary
            for match in __import__("re").finditer(r"\b(\d+)\b", question.question):
                slot_values.setdefault("n", [])
                if int(match.group(1)) not in slot_values["n"] and 1 < int(match.group(1)) <= 60:
                    slot_values["n"].append(int(match.group(1)))
        config["expansion"] = {"model": expand, "per_term": per_term, "per_example": per_example, "slot_values": slot_values}
    prebuilt = None
    if from_seeds:
        import yaml

        from sqlgami.fixtures import from_connection

        from . import build

        extra = yaml.safe_load(rules_file.read_text(encoding="utf-8")) if rules_file else {}
        where = (extra or {}).get("subsample") or rules.get("subsample") or {}  # the suite's own recipe when no file is given
        prebuilt = []
        for s in suite.nasty_seeds() or suite.seeds:
            conn = build.connect(suite, engine, s)
            try:
                prebuilt.append(from_connection(conn, catalog, f"seed-{s.seed}", where=where, profile="subsample", seed=s.seed))
            finally:
                conn.close()
    bundle = compile_bundle(config, out or bundle_dir(suite, engine), catalog=catalog, fixtures=prebuilt)
    _measure_discrimination(bundle, suite, engine)
    if expand and accept:
        _accept_expansion(bundle)
    return bundle


def _accept_expansion(bundle: Bundle) -> None:
    """Add the accepted wordings to the bundle's copy of the Terms/KPIs sheets, as a host would
    after reviewing ``review/Terms.additions.csv``; recorded in the manifest."""
    import csv
    import io
    import json

    review = bundle.path / "review" / "Terms.additions.csv"
    if not review.exists():
        return
    additions: dict[str, list[str]] = {}
    for row in csv.DictReader(io.StringIO(review.read_text(encoding="utf-8"))):
        additions.setdefault(row["term"].lower(), []).append(row["phrase"])
    if not additions:
        return
    for sheet, column in (("Terms.csv", "term"), ("KPIs.csv", "kpi")):
        path = bundle.path / "terms" / sheet
        if not path.exists():
            continue
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
        if not rows:
            continue
        for row in rows:
            extra = additions.get((row.get(column) or "").lower())
            if extra:
                row["phrases"] = ";".join([p for p in (row.get("phrases") or "").split(";") if p] + extra)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        path.write_text(buffer.getvalue(), encoding="utf-8")
    manifest_path = bundle.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["expansion_accepted"] = True
    manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    bundle.manifest = manifest


def _measure_discrimination(bundle: Bundle, suite: Suite, engine: str) -> None:
    """How many of the suite's planted bugs each fixture tells apart from its gold: written into
    the manifest so live scoring tries the most discriminating fixture first."""
    import json

    from sqlgami.bundle import _run_on

    bugs = [b for b in suite.bugs if b.sql and (b.engine in (None, engine))]
    if not bugs:
        return
    by_key = {r.key: r for r in bundle.references}
    scores: dict[str, int] = {}
    for entry in bundle.manifest.get("fixtures") or []:
        name = entry["name"]
        conn = bundle.connection(name)
        diverged = 0
        for bug in bugs:
            reference = by_key.get(bug.question_id)
            stored = reference.results.get(name) if reference else None
            if not stored or not stored.get("ok") or not stored.get("row_count"):
                continue
            question = suite.question(bug.question_id)
            params = question.param_values() if question else {}
            result = _run_on(conn, bug.sql, engine, params or {}, timeout_s=10.0, slots=reference.slots, allowed_functions=(suite.config.get("sqlgami") or {}).get("allowed_functions") or ())
            if not result.get("ok") or (result["strict"] != stored["strict"] and result["loose"] != stored["loose"]):
                diverged += 1
        scores[name] = diverged
        entry["discrimination"] = diverged
    bundle.manifest["bugs_measured"] = len(bugs)
    (bundle.path / "manifest.json").write_text(json.dumps(bundle.manifest, indent=1), encoding="utf-8")
