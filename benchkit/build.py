"""Generate a suite's data and load it into a database per engine and seed.

Everything written here goes under ``bench/.data/``, which is gitignored.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from .suite import DATA_DIR, Seed, Suite, SuiteError

SUPPORTED = ("sqlite", "duckdb")


def data_dir(suite: Suite, seed: Seed) -> Path:
    return DATA_DIR / suite.name / "rows" / seed.label


def db_path(suite: Suite, engine: str, seed: Seed) -> Path:
    suffix = {"sqlite": "sqlite", "duckdb": "duckdb"}[engine]
    return DATA_DIR / suite.name / "db" / f"{seed.label}.{suffix}"


# ---------------------------------------------------------------- generate


def generate(suite: Suite, seed: Seed, out: Path | None = None, force: bool = False) -> Path:
    """Run the suite's generator for one seed. Reuses existing output unless ``force``."""
    out = out or data_dir(suite, seed)
    marker = out / ".complete"
    if marker.exists() and not force:
        return out
    script = suite.path / "generate.py"
    if not script.exists():
        raise SuiteError(f"{suite.name} has no generate.py")
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.jsonl"):
        old.unlink()
    # -I keeps the generator from importing anything out of its own or the current directory.
    result = subprocess.run(
        [sys.executable, "-I", str(script), "--seed", str(seed.seed), "--profile", seed.profile, "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise SuiteError(f"generate.py failed for seed {seed.seed}: {result.stderr.strip()[-800:]}")
    marker.write_text("ok\n")
    return out


def digest(directory: Path) -> str:
    """One hash over every data file, to test that generation is repeatable."""
    sha = hashlib.sha256()
    for path in sorted(directory.glob("*.jsonl")):
        sha.update(path.name.encode())
        sha.update(path.read_bytes())
    return sha.hexdigest()


def row_counts(directory: Path) -> dict[str, int]:
    counts = {}
    for path in sorted(directory.glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            counts[path.stem] = sum(1 for line in handle if line.strip())
    return counts


def read_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# -------------------------------------------------------------------- load


def split_statements(sql: str) -> list[str]:
    """Split DDL text on semicolons, ignoring those inside strings and comments."""
    statements: list[str] = []
    current: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end
            continue
        if ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch in ("'", '"'):
            end = i + 1
            while end < n:
                if sql[end] == ch:
                    if end + 1 < n and sql[end + 1] == ch:
                        end += 2
                        continue
                    break
                end += 1
            current.append(sql[i : end + 1])
            i = end + 1
            continue
        if ch == ";":
            text = "".join(current).strip()
            if text:
                statements.append(text)
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    text = "".join(current).strip()
    if text:
        statements.append(text)
    return statements


def table_name(engine: str, stem: str) -> str:
    """The SQL name of the table a data file belongs to (file stem is ``schema.table``)."""
    schema, _, table = stem.rpartition(".")

    def quote(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    if engine == "sqlite":
        return quote(f"{schema}_{table}" if schema else table)
    return f"{quote(schema)}.{quote(table)}" if schema else quote(table)


def _encode(value: Any, engine: str) -> Any:
    if isinstance(value, bool):
        return int(value) if engine == "sqlite" else value
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return value


def _insert_order(stems: list[str], references: dict[str, set[str]]) -> list[str]:
    """Parents before children, so engines that always enforce foreign keys accept the load."""
    ordered: list[str] = []
    placed: set[str] = set()
    remaining = list(stems)
    while remaining:
        progressed = False
        for stem in list(remaining):
            needs = {r for r in references.get(stem, set()) if r != stem and r in stems}
            if needs <= placed:
                ordered.append(stem)
                placed.add(stem)
                remaining.remove(stem)
                progressed = True
        if not progressed:  # a cycle: load the rest in name order and let the engine decide
            ordered.extend(remaining)
            break
    return ordered


_REFERENCE = re.compile(r'REFERENCES\s+((?:"[^"]+"|\w+)(?:\.(?:"[^"]+"|\w+))?)', re.IGNORECASE)
_CREATE_TABLE = re.compile(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?((?:"[^"]+"|\w+)(?:\.(?:"[^"]+"|\w+))?)', re.IGNORECASE)


def _norm(name: str) -> str:
    return name.replace('"', "").lower()


def _references(statements: Iterable[str], engine: str) -> dict[str, set[str]]:
    """table -> tables it references, keyed the way data file stems are spelled (lower-cased)."""
    result: dict[str, set[str]] = {}
    for statement in statements:
        match = _CREATE_TABLE.match(statement.strip())
        if not match:
            continue
        table = _norm(match.group(1))
        result[table] = {_norm(r) for r in _REFERENCE.findall(statement)}
    return result


def _stem_key(engine: str, stem: str) -> str:
    schema, _, table = stem.rpartition(".")
    if engine == "sqlite":
        return f"{schema}_{table}".lower() if schema else table.lower()
    return stem.lower()


def build(suite: Suite, engine: str, seed: Seed, force: bool = False) -> Path:
    """Create and fill the database for one engine and seed. Returns its path."""
    if engine not in SUPPORTED:
        raise SuiteError(f"benchkit cannot build {engine} databases yet")
    path = db_path(suite, engine, seed)
    if path.exists() and not force:
        return path
    rows_dir = generate(suite, seed)
    schema_file = suite.schema_file(engine)
    if not schema_file.exists():
        raise SuiteError(f"{suite.name} has no schema for {engine}")
    statements = split_statements(schema_file.read_text(encoding="utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    for leftover in (partial, path):
        if leftover.exists():
            leftover.unlink()

    references = _references(statements, engine)
    by_key = {_stem_key(engine, p.stem): p for p in sorted(rows_dir.glob("*.jsonl"))}
    order = _insert_order(sorted(by_key), references)

    if engine == "sqlite":
        conn = sqlite3.connect(partial)
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            for statement in statements:
                if statement.upper().startswith("PRAGMA FOREIGN_KEYS"):
                    continue
                try:
                    conn.execute(statement)
                except sqlite3.Error as exc:
                    raise SuiteError(f"sqlite schema statement failed: {exc}\n{statement[:200]}") from exc
            for key in order:
                _load_file(conn, engine, by_key[key])
            conn.commit()
        finally:
            conn.close()
    else:
        import duckdb

        conn = duckdb.connect(str(partial))
        try:
            try:
                conn.execute("LOAD icu")
            except duckdb.Error as exc:
                raise SuiteError(
                    "DuckDB's ICU extension is not available offline; time-zone gold SQL needs it "
                    f"({exc})"
                ) from exc
            conn.execute("SET TimeZone='UTC'")
            for statement in statements:
                head = statement.upper()
                if head.startswith("INSTALL ") or head.startswith("LOAD "):
                    continue  # never download an extension during a build
                try:
                    conn.execute(statement)
                except duckdb.Error as exc:
                    raise SuiteError(f"duckdb schema statement failed: {exc}\n{statement[:200]}") from exc
            conn.execute("BEGIN")
            for key in order:
                _load_file(conn, engine, by_key[key])
            conn.execute("COMMIT")
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
    partial.rename(path)
    return path


def _load_file(conn: Any, engine: str, path: Path) -> None:
    rows = read_rows(path)
    if not rows:
        return
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    target = table_name(engine, path.stem)
    names = ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
    marks = ", ".join("?" for _ in columns)
    statement = f"INSERT INTO {target} ({names}) VALUES ({marks})"
    values = [[_encode(row.get(c), engine) for c in columns] for row in rows]
    try:
        conn.executemany(statement, values)
    except Exception as exc:
        raise SuiteError(f"loading {path.name} into {engine} failed: {exc}") from exc


def connect(suite: Suite, engine: str, seed: Seed) -> Any:
    """A read-only connection to a built database, with the session pinned to UTC."""
    path = build(suite, engine, seed)
    if engine == "sqlite":
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    import duckdb

    conn = duckdb.connect(str(path), read_only=True)
    conn.execute("LOAD icu")
    conn.execute("SET TimeZone='UTC'")
    return conn
