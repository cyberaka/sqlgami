"""The compiled bundle (ADR 0002): what live scoring loads instead of calling anything.

``compile_bundle(config, out)`` runs OFFLINE. From the host's schema, term catalogue and
reference queries (the catalogue's Examples sheet, plus any ``references:`` in the config) it:

1. builds the adversarial fixture datasets (:mod:`sqlgami.fixtures`) from the schema and the
   declared rules;
2. runs every reference query on every fixture and stores the result fingerprints (and a
   bounded number of rows, for the evidence a mismatch shows);
3. writes a plain, versioned, reviewable folder: ``manifest.json``, ``schema.sql``, ``terms/``,
   ``fixtures/<name>/``, ``references.json``, ``rejected.jsonl``.

A reference enters the bundle only when its query runs on every fixture; one that fails is
written to ``rejected.jsonl`` for the host to look at. No model is called here (a later step
may let an offline model propose variant questions and queries, which enter the bundle only
when they agree with the host's references on every fixture).

Live, ``Scorer(bundle="path/")`` loads the bundle — nothing else: the schema hash is checked
against the live catalogue, the fixtures are loaded into in-memory DuckDB on first use, and a
candidate for a question with a reference is run on the fixtures (while the deadline allows)
and compared with the stored fingerprints.

Config (YAML or dict):

```yaml
dialect: duckdb
schema: schema/duckdb.sql            # DDL text file, or a connection the CLI opens
terms: metadata/terms/               # catalogue folder or .xlsx; its Examples are references
snapshot_columns: {hr.emp_period_snapshot: snap_dt}
required_predicates: [...]           # as for Scorer
fixtures: {seed: 1, profiles: [...], rows: {...}, params: {...}, critical_rows: {...}}
references:                          # optional, in addition to the Examples sheet
  - {key: q017, question: "...", sql: "...", params: {manager_id: 12003}}
```
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import sqlglot

from . import __version__ as _version
from .catalog import Catalog, RequiredPredicate
from .consistency import _norm, fingerprint
from .errors import ConfigurationError
from .fixtures import Fixture, FixtureRules, build
from .terms import Catalogue, load_terms, normalise

BUNDLE_VERSION = 1
MAX_STORED_ROWS = 50


@dataclass
class Reference:
    key: str
    question: str
    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    terms: tuple[str, ...] = ()  # names of the catalogue terms the question matched: the question's PATTERN
    slots: dict[str, Any] = field(default_factory=dict)  # numbers the question bound into those terms ({n: 26})
    results: dict[str, dict[str, Any]] = field(default_factory=dict)  # fixture name -> stored result

    @property
    def question_key(self) -> str:
        return normalise(self.question).strip()

    @property
    def slot_names_in_sql(self) -> set[str]:
        """Slots the reference SQL is written over (``{n}``), so it can be re-run for other values."""
        import re

        return set(re.findall(r"\{(n|m)\}", self.sql))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "question": self.question, "sql": self.sql, "params": self.params,
            "terms": list(self.terms), "slots": self.slots, "results": self.results,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Reference":
        return cls(
            str(data["key"]), str(data["question"]), str(data["sql"]), dict(data.get("params") or {}),
            tuple(data.get("terms") or ()), dict(data.get("slots") or {}), dict(data.get("results") or {}),
        )


def _fingerprint_text(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> tuple[str, str]:
    strict, loose = fingerprint(rows)
    return (
        hashlib.sha256(json.dumps([list(columns), strict], default=str).encode("utf-8")).hexdigest(),
        hashlib.sha256(json.dumps(loose, default=str).encode("utf-8")).hexdigest(),
    )


def schema_hash(catalog: Catalog) -> str:
    """A hash of the base tables, their columns (by type family, so INTEGER and BIGINT, TEXT and
    VARCHAR agree between a DDL file and a live catalogue) and their primary keys: the bundle is
    tied to it. Views are left out: a live catalogue lists their columns, a DDL file does not."""
    from .types import family_of_declared

    h = hashlib.sha256()
    for name in catalog.table_names():
        schema, _, bare = name.rpartition(".")
        table = catalog.resolve(bare, schema or None, quoted=True) or catalog.resolve(bare, schema or None)
        if table is None or table.is_view:
            continue
        h.update(name.lower().encode("utf-8"))
        for column in sorted(table.columns, key=lambda c: c.name.lower()):
            h.update(f"{column.name.lower()}:{family_of_declared(column.type or '', catalog.dialect)}".encode("utf-8"))
        h.update(json.dumps(sorted(c.lower() for c in table.primary_key)).encode("utf-8"))
    return h.hexdigest()


def _plain(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return _norm(value)


# ------------------------------------------------------------------ compiling


def compile_bundle(
    config: dict[str, Any] | str | Path, out: str | Path, *, catalog: Catalog | None = None, fixtures: Sequence[Fixture] | None = None
) -> "Bundle":
    """Build the bundle folder at ``out`` from ``config`` (a dict, YAML text, or a YAML file).
    ``fixtures`` replaces the generated ones with fixtures built elsewhere (a subsample)."""
    if isinstance(config, (str, Path)) and not str(config).lstrip().startswith(("{", "dialect:", "schema:")) and Path(config).exists():
        base = Path(config).parent
        config = _load_yaml(Path(config).read_text(encoding="utf-8"))
    elif isinstance(config, str):
        base = Path(".")
        config = _load_yaml(config)
    else:
        base = Path(".")
    if not isinstance(config, dict):
        raise ConfigurationError("the compile config must be a mapping")
    dialect = str(config.get("dialect") or "duckdb")
    if catalog is None:
        schema = config.get("schema")
        if not schema:
            raise ConfigurationError("the compile config needs 'schema': a DDL file")
        path = Path(schema) if Path(schema).is_absolute() else base / schema
        catalog = Catalog.from_ddl(path.read_text(encoding="utf-8"), dialect, snapshot_columns=config.get("snapshot_columns") or None)
    elif config.get("snapshot_columns"):
        catalog.snapshot_columns.update({k.lower(): v for k, v in config["snapshot_columns"].items()})
    for rule in config.get("required_predicates") or []:
        catalog.required_predicates.append(RequiredPredicate.coerce(rule))

    terms_source = config.get("terms")
    catalogue: Catalogue | None = None
    terms_path: Path | None = None
    if terms_source:
        terms_path = Path(terms_source) if Path(terms_source).is_absolute() else base / terms_source
        catalogue = load_terms(terms_path)

    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "schema.sql").write_text(_ddl_text(catalog), encoding="utf-8")
    if terms_path is not None:
        if terms_path.is_dir():
            shutil.copytree(terms_path, out / "terms")
        else:
            (out / "terms").mkdir()
            shutil.copy(terms_path, out / "terms" / terms_path.name)

    rules = FixtureRules.from_dict(config.get("fixtures") or {})
    fixtures = list(fixtures) if fixtures is not None else build(catalog, rules)
    for fixture in fixtures:
        fixture.write(out / "fixtures" / fixture.name)

    references = _references(config, catalogue)
    kept: list[Reference] = []
    rejected: list[dict[str, Any]] = []
    timings: dict[str, list[float]] = {f.name: [] for f in fixtures}
    connections = {f.name: f.connect() for f in fixtures}
    try:
        for reference in references:
            problems: list[str] = []
            for fixture in fixtures:
                started = time.perf_counter()
                outcome = _run_on(connections[fixture.name], reference.sql, dialect, reference.params, slots=reference.slots)
                timings[fixture.name].append((time.perf_counter() - started) * 1000)
                if outcome.get("error"):
                    problems.append(f"{fixture.name}: {outcome['error']}")
                reference.results[fixture.name] = outcome
            if problems:
                rejected.append({"key": reference.key, "question": reference.question, "problems": problems})
            else:
                kept.append(reference)
    finally:
        for conn in connections.values():
            conn.close()
    (out / "references.json").write_text(json.dumps([r.to_dict() for r in kept], indent=1, default=str), encoding="utf-8")
    (out / "rejected.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rejected), encoding="utf-8")
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "sqlgami_version": _version,
        "created": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "dialect": dialect,
        "schema_hash": schema_hash(catalog),
        "catalogue": catalogue.to_dict() if catalogue is not None else None,
        "fixtures": [
            {
                "name": f.name,
                "profile": f.profile,
                "seed": f.seed,
                "rows": f.row_count(),
                "digest": f.digest(),
                "reference_ms": round(sorted(timings[f.name])[len(timings[f.name]) // 2], 2) if timings[f.name] else None,
                "reference_max_ms": round(max(timings[f.name]), 2) if timings[f.name] else None,
            }
            for f in fixtures
        ],
        "params": rules.params,
        "references": len(kept),
        "rejected": len(rejected),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return Bundle.load(out)


def _load_yaml(text: str) -> Any:
    import yaml

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"the compile config is not valid YAML: {exc}") from exc


def _ddl_text(catalog: Catalog) -> str:
    from .consistency import describe_schema

    return describe_schema(catalog, limit=10_000)


def _references(config: dict[str, Any], catalogue: Catalogue | None) -> list[Reference]:
    found: list[Reference] = []
    if catalogue is not None:
        for example in catalogue.examples:
            if not example.correct_sql.strip():
                continue
            found.append(Reference(example.id, example.question, example.correct_sql, _parse_params(example.params)))
    for row in config.get("references") or []:
        if not isinstance(row, dict) or not row.get("sql") or not row.get("question"):
            continue
        found.append(Reference(str(row.get("key") or f"ref{len(found) + 1}"), str(row["question"]), str(row["sql"]), dict(row.get("params") or {})))
    seen: set[str] = set()
    unique: list[Reference] = []
    for reference in found:
        if reference.key in seen:
            raise ConfigurationError(f"two references share the key {reference.key!r}")
        seen.add(reference.key)
        if catalogue is not None:
            matches = catalogue.match(reference.question)
            reference.terms = tuple(sorted({m.name.lower() for m in matches}))
            reference.slots = slot_values(matches)
        unique.append(reference)
    return unique


def slot_values(matches: Sequence[Any]) -> dict[str, Any]:
    """The numbers a question binds into its terms' ``{n}``/``{m}`` slots: ``{"n": 26}``. A slot
    bound to two different numbers in one question is ambiguous and dropped."""
    from .terms import bound_number

    found: dict[str, Any] = {}
    clashed: set[str] = set()
    for match in matches:
        for slot, text in (match.bindings or {}).items():
            value = bound_number(text)
            if value is None:
                continue
            if slot in found and found[slot] != value:
                clashed.add(slot)
            found[slot] = value
    for slot in clashed:
        found.pop(slot, None)
    return found


def _parse_params(text: str) -> dict[str, Any]:
    """``manager_id=12003; codes=US,GB`` or a JSON object."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            return dict(json.loads(text))
        except ValueError:
            return {}
    out: dict[str, Any] = {}
    for part in text.split(";"):
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        value = value.strip()
        if "," in value:
            out[name.strip()] = [_scalar(v) for v in value.split(",")]
        else:
            out[name.strip()] = _scalar(value)
    return out


def _scalar(text: str) -> Any:
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _executable(sql: str, dialect: str, params: dict[str, Any], slots: dict[str, Any] | None = None) -> str:
    from . import template

    for slot, value in (slots or {}).items():
        sql = sql.replace("{" + slot + "}", str(int(value)) if isinstance(value, (int, float)) and float(value).is_integer() else str(value))
    prepared = template.prepare(sql, dialect, params or None)
    text = prepared.executable_sql or sql
    if dialect != "duckdb":
        text = sqlglot.transpile(text, read=dialect, write="duckdb")[0]
    return text


def _run_on(conn: Any, sql: str, dialect: str, params: dict[str, Any], timeout_s: float | None = None, slots: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run ``sql`` on a fixture connection and return the stored form of its result."""
    from .engines.duckdb import DuckDBAdapter

    try:
        text = _executable(sql, dialect, params, slots)
    except Exception as exc:
        return {"ok": False, "error": f"could not prepare: {exc}"}
    adapter = DuckDBAdapter(conn)
    result = adapter.execute(text, max_rows=10_000, timeout_s=timeout_s or 30.0)
    if not result.ok:
        return {"ok": False, "error": result.error or "failed"}
    strict, loose = _fingerprint_text(result.columns, result.rows)
    return {
        "ok": True,
        "columns": list(result.columns),
        "row_count": len(result.rows),
        "truncated": result.truncated,
        "strict": strict,
        "loose": loose,
        "rows": [_plain(r) for r in result.rows[:MAX_STORED_ROWS]],
    }


# ------------------------------------------------------------------ loading


class Bundle:
    def __init__(self, path: Path, manifest: dict[str, Any], references: list[Reference]) -> None:
        self.path = path
        self.manifest = manifest
        self.references = references
        self._fixtures: dict[str, Fixture] | None = None
        self._connections: dict[str, Any] = {}
        self._by_question = {r.question_key: r for r in references}
        self._by_key = {r.key: r for r in references}
        by_terms: dict[tuple[str, ...], list[Reference]] = {}
        for r in references:
            if r.terms:
                by_terms.setdefault(r.terms, []).append(r)
        self._by_terms = by_terms  # a pattern (term set) -> its references; usable only when exactly one

    @classmethod
    def load(cls, path: str | Path) -> "Bundle":
        folder = Path(path)
        manifest_file = folder / "manifest.json"
        if not manifest_file.exists():
            raise ConfigurationError(f"{folder} is not a bundle (no manifest.json)")
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if int(manifest.get("bundle_version", 0)) != BUNDLE_VERSION:
            raise ConfigurationError(f"bundle version {manifest.get('bundle_version')} is not {BUNDLE_VERSION}; recompile it")
        references = [Reference.from_dict(r) for r in json.loads((folder / "references.json").read_text(encoding="utf-8"))]
        return cls(folder, manifest, references)

    @property
    def dialect(self) -> str:
        return str(self.manifest.get("dialect") or "duckdb")

    @property
    def schema_hash(self) -> str:
        return str(self.manifest.get("schema_hash") or "")

    def terms_path(self) -> Path | None:
        folder = self.path / "terms"
        if folder.is_dir():
            books = list(folder.glob("*.xlsx"))
            return books[0] if books and not list(folder.glob("*.csv")) else folder
        return None

    def fixtures(self) -> dict[str, Fixture]:
        if self._fixtures is None:
            self._fixtures = {}
            for entry in self.manifest.get("fixtures") or []:
                fixture = Fixture.read(self.path / "fixtures" / entry["name"])
                if fixture.digest() != entry.get("digest"):
                    raise ConfigurationError(f"fixture {entry['name']} does not match the manifest; recompile the bundle")
                self._fixtures[entry["name"]] = fixture
        return self._fixtures

    def connection(self, name: str) -> Any:
        """An in-memory DuckDB holding the fixture, opened on first use and kept."""
        if name not in self._connections:
            self._connections[name] = self.fixtures()[name].connect()
        return self._connections[name]

    def warm(self) -> None:
        """Load every fixture now (a second or so for a few thousand rows each), so the first
        score() call does not pay for it."""
        for entry in self.manifest.get("fixtures") or []:
            self.connection(entry["name"])

    def fixture_order(self) -> list[dict[str, Any]]:
        """Fixtures in the order to try them live: the ones whose references took longest last."""
        entries = list(self.manifest.get("fixtures") or [])
        return sorted(entries, key=lambda e: (e.get("reference_max_ms") or 0.0))

    def reference_for(self, question: str | None, key: str | None = None, terms: Sequence[str] = ()) -> tuple[Reference | None, str]:
        """The reference for a question: by explicit key, by the exact question text, or by the
        question's PATTERN — the exact set of catalogue terms it matched — when that pattern
        belongs to one reference only. Returns ``(reference, how)``; ``how`` says why not."""
        if key:
            found = self._by_key.get(key)
            return (found, "key") if found else (None, f"no reference with key {key!r}")
        if question:
            found = self._by_question.get(normalise(question).strip())
            if found is not None:
                return found, "question"
        if terms:
            candidates = self._by_terms.get(tuple(sorted(t.lower() for t in terms))) or []
            if len(candidates) == 1:
                return candidates[0], "pattern"
            if len(candidates) > 1:
                keys = ", ".join(r.key for r in candidates[:4])
                return None, f"the question's pattern matches {len(candidates)} references ({keys}); register a distinguishing term or name the reference"
        return None, "no reference matches this question"

    def close(self) -> None:
        for conn in self._connections.values():
            try:
                conn.close()
            except Exception:
                pass
        self._connections.clear()
