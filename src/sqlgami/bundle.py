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
from typing import Any, Mapping, Sequence

import sqlglot

from . import __version__ as _version
from .catalog import Catalog, RequiredPredicate
from .consistency import _norm, fingerprint
from .errors import ConfigurationError
from .fixtures import Fixture, FixtureRules, build
from .terms import Catalogue, load_terms, normalise
from .translate import column_types_of

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
    variant_of: str = ""  # a paraphrase of another reference (same SQL, its own question and slots)

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
            "terms": list(self.terms), "slots": self.slots, "results": self.results, "variant_of": self.variant_of,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Reference":
        return cls(
            str(data["key"]), str(data["question"]), str(data["sql"]), dict(data.get("params") or {}),
            tuple(data.get("terms") or ()), dict(data.get("slots") or {}), dict(data.get("results") or {}),
            str(data.get("variant_of") or ""),
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
        if table is None or table.is_view or table.is_table_function:
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
    config: dict[str, Any] | str | Path,
    out: str | Path,
    *,
    catalog: Catalog | None = None,
    fixtures: Sequence[Fixture] | None = None,
    chat: Any = None,
) -> "Bundle":
    """Build the bundle folder at ``out`` from ``config`` (a dict, YAML text, or a YAML file).
    ``fixtures`` replaces the generated ones with fixtures built elsewhere (a subsample).

    With an ``expansion:`` block (``model: hf:<name>``, ``per_term``, ``per_example``) — or a
    ``chat`` callable — a language model proposes synonym phrases and paraphrased questions
    offline; see :mod:`sqlgami.expand` for what is kept. Accepted phrases go to
    ``review/Terms.additions.csv`` for the host; accepted variants are run on every fixture
    and stored as references of their own. This is the only place a model is ever called."""
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
    unique_keys = unique_key_facts(config)
    apply_unique_keys(catalog, unique_keys)

    terms_source = config.get("terms")
    catalogue: Catalogue | None = None
    terms_path: Path | None = None
    if terms_source:
        terms_path = Path(terms_source) if Path(terms_source).is_absolute() else base / terms_source
        catalogue = load_terms(terms_path, dialect)

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

    # The host's names for the fixture tables: what it declared under tables:, plus every
    # three-part name the catalogue itself declares (```proj.ds.t``` is ds.t in the fixtures).
    from .fixtures import logical_name

    table_map: dict[str, str] = {}
    for qualified in catalog.table_names():
        if qualified.count(".") >= 2:
            table_map[qualified.strip("`").lower()] = logical_name(qualified)
    table_map.update({str(k).strip("`").lower(): str(v) for k, v in (config.get("tables") or {}).items()})
    config = {**config, "tables": table_map}

    rules = FixtureRules.from_dict(config.get("fixtures") or {})
    fixtures = list(fixtures) if fixtures is not None else build(catalog, rules)
    for fixture in fixtures:
        fixture.write(out / "fixtures" / fixture.name)

    references = _references(config, catalogue)
    kept: list[Reference] = []
    rejected: list[dict[str, Any]] = []
    expansion_summary: dict[str, Any] | None = None
    expansion_config = dict(config.get("expansion") or {})
    if (chat is not None or expansion_config.get("model")) and catalogue is not None:
        from .expand import chat_for, expand

        model = str(expansion_config.get("model") or getattr(chat, "model", "") or "")
        chat = chat if chat is not None else chat_for(model)
        expansion = expand(
            catalogue,
            references,
            chat,
            model=model,
            per_term=int(expansion_config.get("per_term", 6)),
            per_example=int(expansion_config.get("per_example", 4)),
            slot_choices=expansion_config.get("slot_values") or {},
        )
        references = list(references) + list(expansion.variants)
        rejected.extend(expansion.rejected)
        (out / "review").mkdir(exist_ok=True)
        (out / "review" / "Terms.additions.csv").write_text(expansion.review_csv(), encoding="utf-8")
        (out / "review" / "README.txt").write_text(
            "Terms.additions.csv lists wordings a language model proposed and sqlgami accepted after "
            "re-matching every registered question deterministically. Nothing here is live until you add "
            "the rows to the Terms sheet of your workbook and compile again. The variants in "
            "references.json were validated against the catalogue with these wordings in place.\n",
            encoding="utf-8",
        )
        expansion_summary = expansion.to_dict()
    timings: dict[str, list[float]] = {f.name: [] for f in fixtures}
    connections = {f.name: f.connect() for f in fixtures}
    try:
        for reference in references:
            problems: list[str] = []
            for fixture in fixtures:
                started = time.perf_counter()
                outcome = _run_on(
                    connections[fixture.name], reference.sql, dialect, reference.params, slots=reference.slots,
                    allowed_functions=config.get("allowed_functions") or (), allowed_tables=config.get("allowed_tables") or (),
                    tables=config.get("tables") or None, functions=catalog.scalar_functions and {k: {"parameters": [list(p) for p in f.parameters], "body": f.body, "language": f.language} for k, f in catalog.scalar_functions.items()} or None,
                    column_types=column_types_of(catalog) or None,
                )
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
        "schema_file": str(config.get("schema") or ""),  # which dialect's DDL the fixtures came from
        "tables": {str(k): str(v) for k, v in (config.get("tables") or {}).items()},  # host names -> fixture tables (ADR 0003)
        # The host's scalar functions: SQL ones are inlined on the fixtures, JavaScript ones make a reader not_run.
        "functions": {k: {"parameters": [list(p) for p in f.parameters], "body": f.body, "language": f.language} for k, f in catalog.scalar_functions.items()},
        "schema_hash": schema_hash(catalog),
        "catalogue": catalogue.to_dict() if catalogue is not None else None,
        "fixtures": [
            {
                "name": f.name,
                "profile": f.profile,
                "seed": f.seed,
                "rows": f.row_count(),
                "digest": f.digest(),
                "views_digest": f.views_digest(),
                "reference_ms": round(sorted(timings[f.name])[len(timings[f.name]) // 2], 2) if timings[f.name] else None,
                "reference_max_ms": round(max(timings[f.name]), 2) if timings[f.name] else None,
            }
            for f in fixtures
        ],
        "params": rules.params,
        "references": len(kept),
        "rejected": len(rejected),
        "expansion": expansion_summary,
        # Views and table functions whose body uses a construct the translation has not verified:
        # a statement reading one gets no fixture verdict.
        "untranslatable_views": dict(__import__("sqlgami.fixtures", fromlist=["UNTRANSLATABLE_VIEWS"]).UNTRANSLATABLE_VIEWS),
        "column_types": column_types_of(catalog),
        "unique_keys": unique_keys,
        # References whose question states a number that no slot or parameter binds ("the two
        # counts"): a question compares under them only when it states the same number, so the
        # host may want to reword the reference, or write that number as a {slot}.
        "references_with_free_numbers": {
            r.key: [{"number": v, "phrase": phrase} for v, phrase in free_number_phrases(r.question, r.slots, r.params or {})]
            for r in kept
            if free_number_phrases(r.question, r.slots, r.params or {})
        },
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


def unique_key_facts(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Uniqueness facts the host states, as ``{table, columns, where}``: the ``unique_keys:``
    block of the compile config, and every ``one_true`` fixture rule (one TRUE row per ``per``
    group is one row per group where the flag is TRUE — a fact about the real table that the
    fixtures were built to honour)."""
    facts: list[dict[str, Any]] = []
    for item in config.get("unique_keys") or []:
        if not isinstance(item, dict) or not item.get("table") or not item.get("columns"):
            raise ConfigurationError(f"unique_keys entry {item!r}: needs table: and columns: [..] (and optionally where: {{column: value}})")
        where = item.get("where")
        if where is not None and (not isinstance(where, dict) or len(where) != 1):
            raise ConfigurationError(f"unique_keys entry {item!r}: where: must be one {{column: value}}")
        facts.append({"table": str(item["table"]), "columns": [str(c) for c in item["columns"]], "where": dict(where) if where else None})
    for rule in (config.get("fixtures") or {}).get("rules") or []:
        if isinstance(rule, dict) and rule.get("kind") == "one_true" and rule.get("column") and rule.get("per"):
            table, _, column = str(rule["column"]).rpartition(".")
            facts.append({"table": table, "columns": [str(c) for c in rule["per"]], "where": {column: True}})
    return facts


def apply_unique_keys(catalog: Catalog, facts: Sequence[Mapping[str, Any]]) -> None:
    """Declare ``facts`` (see :func:`unique_key_facts`) on ``catalog``; a fact about a table the
    catalogue does not have is skipped (a rule may name a table of another engine's spelling)."""
    from .catalog import CatalogError

    for fact in facts:
        table = str(fact["table"])
        schema, _, name = table.rpartition(".")
        where = fact.get("where") or None
        condition = next(iter(where.items())) if where else None
        try:
            catalog.declare_unique(name, list(fact["columns"]), schema or None, where=condition)
        except CatalogError:
            try:
                catalog.declare_unique(table, list(fact["columns"]), None, where=condition)
            except CatalogError:
                continue


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
        question = str(row["question"])
        # A templated reference may write its slots into the question too ("last {n} pay
        # periods"); its defaults make the question concrete for matching, the SQL stays templated.
        defaults = dict(row.get("slots") or {})
        if row.get("n_default") is not None:
            defaults.setdefault("n", row["n_default"])
        for slot, value in defaults.items():
            question = question.replace("{" + slot + "}", str(value))
        params = row.get("params") or {}
        if isinstance(params, (list, tuple)):
            params = {}  # names only: the values come from the question or the caller
        reference = Reference(str(row.get("key") or row.get("id") or f"ref{len(found) + 1}"), question, str(row["sql"]), dict(params))
        reference.slots = {k: v for k, v in defaults.items()}  # the declared defaults; the question's own bindings win below
        found.append(reference)
    seen: set[str] = set()
    unique: list[Reference] = []
    for reference in found:
        if reference.key in seen:
            raise ConfigurationError(f"two references share the key {reference.key!r}")
        seen.add(reference.key)
        if catalogue is not None:
            matches = catalogue.match(reference.question)
            reference.terms = catalogue.pattern(reference.question)
            reference.slots = {**reference.slots, **slot_values(matches)}
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


def free_number_phrases(question: str, slots: dict[str, Any], params: dict[str, Any]) -> list[tuple[float, str]]:
    """The numbers a question states that neither a term slot nor a parameter accounts for, each
    with the few words around it ("if four nodes tie"), so a host can see which wording is meant.
    Digits and number words from two up count ("one" is prose); years are dates, not quantities."""
    import re

    from .terms import _NUMBER_WORDS, normalise

    tokens = normalise(question).split()
    found: list[tuple[float, str]] = []
    for index, token in enumerate(tokens):
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
            value = float(token)
        elif token in _NUMBER_WORDS and _NUMBER_WORDS[token] != "1":
            value = float(_NUMBER_WORDS[token])
        else:
            continue
        if 1900 <= value <= 2100:
            continue
        found.append((value, " ".join(tokens[max(0, index - 3) : index + 3])))
    for value in list(slots.values()) + list(params.values()):
        for v in value if isinstance(value, (list, tuple)) else [value]:
            try:
                number = float(v)
            except (TypeError, ValueError):
                continue
            for entry in found:
                if entry[0] == number:
                    found.remove(entry)
                    break
    return sorted(found)


def free_numbers(question: str, slots: dict[str, Any], params: dict[str, Any]) -> tuple:
    """The free numbers alone, as a sorted tuple (with repeats): two questions compare under a
    reference only when these agree."""
    return tuple(v for v, _ in free_number_phrases(question, slots, params))


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


def _executable(sql: str, dialect: str, params: dict[str, Any], slots: dict[str, Any] | None = None, tables: Mapping[str, str] | None = None, functions: Mapping[str, Any] | None = None, column_types: Mapping[str, str] | None = None) -> str:
    """The statement as DuckDB SQL for a fixture: slots and parameters bound, and — for another
    dialect — translated through the verified allow-list (:mod:`sqlgami.translate`), raising
    ``Untranslatable`` for a construct whose translation is not proven."""
    from . import analysis as A
    from . import template
    from .translate import Untranslatable, column_types_of, translate

    for slot, value in (slots or {}).items():
        sql = sql.replace("{" + slot + "}", str(int(value)) if isinstance(value, (int, float)) and float(value).is_integer() else str(value))
    prepared = template.prepare(sql, dialect, params or None)
    text = prepared.executable_sql or sql
    if dialect == "duckdb":
        return text
    # Parsed once: a script becomes its final query with the variables bound (ADR 0003 B1), a
    # statement is translated as the tree it parsed to.
    statements = [st for st in sqlglot.parse(text, read=dialect) if st is not None]
    if len(statements) > 1 and dialect in A._SCRIPT_DIALECTS and A._is_script(statements):
        script = A.bind_script(statements, dialect, A._statement_defined_names(statements[-1]))
        if script.unbound:
            raise Untranslatable("script variable " + ", ".join(script.unbound) + " has no value the script gives it (no DEFAULT or SET)")
        tree = script.query
    elif len(statements) == 1:
        tree = statements[0]
    else:
        return translate(text, dialect, tables, functions=functions, column_types=column_types).sql
    return translate(tree, dialect, tables, functions=functions, column_types=column_types, source_text=text).sql


def _run_on(
    conn: Any,
    sql: str,
    dialect: str,
    params: dict[str, Any],
    timeout_s: float | None = None,
    slots: dict[str, Any] | None = None,
    allowed_functions: Sequence[str] = (),
    allowed_tables: Sequence[str] = (),
    grace_s: float | None = None,
    tables: Mapping[str, str] | None = None,
    executable: str | None = None,
    functions: Mapping[str, Any] | None = None,
    column_types: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run ``sql`` on a fixture connection and return the stored form of its result. The same
    gate as everywhere applies: a statement that is not a single read-only query over known
    functions and tables is not sent. ``grace_s`` bounds how long past ``timeout_s`` the engine
    is given to stop before the cursor is abandoned (a live deadline passes what it has left).
    ``executable`` is the already-translated DuckDB text, when the caller prepared it once for
    several fixtures."""
    from .engines.duckdb import DuckDBAdapter
    from .errors import UnsafeQueryError
    from .translate import Untranslatable

    try:
        text = executable if executable is not None else _executable(sql, dialect, params, slots, tables, functions, column_types)
    except Untranslatable as exc:
        return {"ok": False, "error": str(exc), "untranslatable": exc.construct}
    except Exception as exc:
        return {"ok": False, "error": f"could not prepare: {exc}"}
    adapter = DuckDBAdapter(conn)
    # The fixture's own table functions (catalogue TVFs materialised as table macros) are
    # read-only by construction: the gate may call them. Everything else is as the host allowed.
    declared = {str(v).split(".")[-1].lower() for v in (tables or {}).values()}
    adapter.allowed_functions = frozenset(n.lower() for n in allowed_functions) | frozenset(declared)
    adapter.allowed_tables = frozenset(n.lower() for n in allowed_tables)
    try:
        result = adapter.execute(text, max_rows=10_000, timeout_s=timeout_s or 30.0, grace_s=grace_s)
    except UnsafeQueryError as exc:
        return {"ok": False, "error": f"not sent: {exc}"}
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


def bundle_hash(folder: str | Path) -> str:
    """A content hash of everything live scoring reads from the bundle — manifest, references,
    the catalogue copy and every fixture file — so a frozen bundle can be named by one string and
    a report says which bundle produced it. Independent of file timestamps and folder location."""
    root = Path(folder)
    h = hashlib.sha256()
    for file in sorted(p for p in root.rglob("*") if p.is_file() and "review" not in p.relative_to(root).parts and p.name != "rejected.jsonl"):
        h.update(str(file.relative_to(root)).replace("\\", "/").encode("utf-8"))
        h.update(b"\0")
        h.update(file.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


class Bundle:
    def __init__(self, path: Path, manifest: dict[str, Any], references: list[Reference]) -> None:
        self.path = path
        self.manifest = manifest
        self.references = references
        self.hash = ""  # the content hash of the folder as loaded (see bundle_hash); reports carry it
        self._fixtures: dict[str, Fixture] | None = None
        self._connections: dict[str, Any] = {}
        self._by_question = {r.question_key: r for r in references}
        self._by_key = {r.key: r for r in references}
        by_terms: dict[tuple[str, ...], list[Reference]] = {}
        for r in references:
            if r.terms and not r.variant_of:  # variants share their base's pattern: the base stands for them
                by_terms.setdefault(r.terms, []).append(r)
        self._by_terms = by_terms  # a pattern (term set) -> its references; usable only when exactly one
        self._variants: dict[str, list[Reference]] = {}
        for r in references:
            if r.variant_of:
                self._variants.setdefault(r.variant_of, []).append(r)

    def stored_for(self, reference: Reference, slots: dict[str, Any], params: dict[str, Any]) -> Reference | None:
        """The reference itself, or one of its compiled variants, whose stored results were
        produced under exactly these slot and parameter values; None when none was."""
        base = self._by_key.get(reference.variant_of) if reference.variant_of else reference
        if base is None:
            return None
        for candidate in [base, *self._variants.get(base.key, [])]:
            if candidate.slots == slots and candidate.params == params:
                return candidate
        return None

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
        bundle = cls(folder, manifest, references)
        bundle.hash = bundle_hash(folder)
        return bundle

    @property
    def dialect(self) -> str:
        return str(self.manifest.get("dialect") or "duckdb")

    @property
    def functions(self) -> dict[str, Any]:
        return dict(self.manifest.get("functions") or {})

    @property
    def untranslatable_views(self) -> dict[str, str]:
        return dict(self.manifest.get("untranslatable_views") or {})

    @property
    def unique_keys(self) -> list[dict[str, Any]]:
        """Uniqueness facts the bundle was compiled with (see :func:`unique_key_facts`)."""
        return list(self.manifest.get("unique_keys") or [])

    @property
    def column_types(self) -> dict[str, str]:
        """Column name → declared type, from the schema the bundle was built for (the
        translation's hint for DATE vs TIMESTAMP and NUMERIC vs FLOAT64 columns)."""
        return dict(self.manifest.get("column_types") or {})

    @property
    def tables(self) -> dict[str, str]:
        """The host's table map: its own names (```proj.ds.t```) to the fixture tables."""
        return dict(self.manifest.get("tables") or {})

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
        """Fixtures in the order to try them live: the most discriminating first (``discrimination``
        in the manifest, when the compile step measured it against known-wrong queries), then the
        ones whose references run fastest."""
        entries = list(self.manifest.get("fixtures") or [])
        return sorted(entries, key=lambda e: (-(e.get("discrimination") or 0.0), e.get("reference_max_ms") or 0.0))

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
