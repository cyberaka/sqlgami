"""Command-line wrapper around :class:`sqlgami.Scorer`. Standard library only.

    sqlgami score --engine sqlite --db app.db --question "..." "SELECT ..."
    sqlgami score --engine duckdb --schema schema.sql --sql-file query.sql --json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .errors import SqlgamiError
from .pipeline import MODES
from .scorer import Scorer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sqlgami",
        description="Advisory confidence report for a SQL statement. Not a guarantee of correctness.",
    )
    parser.add_argument("--version", action="version", version=f"sqlgami {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    score = sub.add_parser("score", help="check one SQL statement")
    score.add_argument("sql", nargs="?", help="the SQL text; omit to use --sql-file or standard input")
    score.add_argument("--engine", required=True, help="sqlite, duckdb, postgres or bigquery")
    score.add_argument("--db", help="path to a database file (opened read-only)")
    score.add_argument("--schema", help="path to a file of CREATE TABLE / CREATE VIEW statements")
    score.add_argument("--sql-file", help="read the SQL from this file")
    score.add_argument("--question", help="the plain-English question the SQL should answer")
    score.add_argument("--mode", choices=MODES, help="how deep to check (default: dry with --db, else static)")
    score.add_argument("--cost-ceiling", type=float, help="do not run queries estimated above this cost")
    score.add_argument("--tenant-column", help="column that separates tenants")
    score.add_argument("--soft-delete-column", help="column that marks soft-deleted rows")
    score.add_argument("--json", action="store_true", help="print the full report as JSON")
    score.add_argument(
        "--fail-under",
        type=float,
        help="exit with status 1 when confidence is below this value (or no number is given)",
    )
    score.add_argument("--bundle", help="a compiled bundle folder (see: sqlgami compile)")
    score.add_argument("--terms", help="a term catalogue: a folder of CSV sheets or an .xlsx workbook")

    compile_ = sub.add_parser("compile", help="build the bundle live scoring loads: fixtures and reference fingerprints (offline; also: train)")
    compile_.add_argument("config", help="a YAML config: dialect, schema, terms, fixtures, references")
    compile_.add_argument("-o", "--out", required=True, help="the bundle folder to write")

    init = sub.add_parser("init", help="write the pilot template (a BigQuery snapshot-style schema, workbook and compile config) into a folder")
    init.add_argument("folder")
    init.add_argument("--force", action="store_true", help="overwrite files that exist")

    doctor = sub.add_parser("doctor", help="check a compile config offline: the workbook, the fixture rules, the table map, and which constructs in your queries the fixture translation has not verified")
    doctor.add_argument("config", help="the compile config (sqlgami.yaml)")
    doctor.add_argument("--queries", help="a .sql file or a folder of .sql files: your own queries, read locally, never executed")
    doctor.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "train":
        argv[0] = "compile"
    args = _parser().parse_args(argv)
    if args.command == "init":
        from .doctor import init_pilot

        try:
            written = init_pilot(args.folder, force=args.force)
        except SqlgamiError as exc:
            print(f"sqlgami: {exc}", file=sys.stderr)
            return 2
        for path in written:
            print(f"wrote {path}")
        print(f"next: edit {args.folder}/schema.sql and {args.folder}/terms/, then: sqlgami doctor {args.folder}/sqlgami.yaml")
        return 0
    if args.command == "doctor":
        from .doctor import _json, diagnose

        try:
            diagnosis = diagnose(args.config, args.queries)
        except SqlgamiError as exc:
            print(f"sqlgami: {exc}", file=sys.stderr)
            return 2
        print(_json(diagnosis) if args.json else diagnosis.text())
        return 0 if diagnosis.ok else 1
    if args.command == "compile":
        from .bundle import compile_bundle

        try:
            bundle = compile_bundle(args.config, args.out)
        except SqlgamiError as exc:
            print(f"sqlgami: {exc}", file=sys.stderr)
            return 2
        manifest = bundle.manifest
        from .bundle import bundle_hash

        print(f"bundle written to {bundle.path}  (hash {bundle_hash(bundle.path)[:16]})")
        print(f"  schema {manifest['schema_hash'][:12]}…, {len(manifest['fixtures'])} fixture(s), {manifest['references']} reference(s), {manifest['rejected']} rejected")
        for entry in manifest["fixtures"]:
            print(f"  {entry['name']:16} {entry['rows']:6} rows   reference query p50 {entry['reference_ms'] or 0:.1f} ms, max {entry['reference_max_ms'] or 0:.1f} ms")
        if manifest["rejected"]:
            print(f"  see {bundle.path / 'rejected.jsonl'}")
        for key, entries in (manifest.get("references_with_free_numbers") or {}).items():
            said = "; ".join(f"{e['number']:g} in \"{e['phrase']}\"" for e in entries)
            print(f"  note: reference {key} states {said} and no slot binds it: only a question stating the same number compares under it — reword without the number, or make it a {{slot}} of a term")
        if expansion := manifest.get("expansion"):
            print(
                f"  expansion ({expansion['model']}): {expansion['proposals']} proposals, {len(expansion['phrases'])} wordings to review in "
                f"{bundle.path / 'review' / 'Terms.additions.csv'}, {len(expansion['variants'])} variants compiled, {expansion['rejected']} rejected"
            )
        return 0
    try:
        if args.sql is not None:
            sql = args.sql
        elif args.sql_file:
            sql = Path(args.sql_file).read_text(encoding="utf-8")
        else:
            sql = sys.stdin.read()
        catalog = Path(args.schema).read_text(encoding="utf-8") if args.schema else None
        with Scorer(
            args.engine,
            db=args.db,
            catalog=catalog,
            bundle=args.bundle,
            terms=args.terms,
            mode=args.mode,
            cost_ceiling=args.cost_ceiling,
            tenant_column=args.tenant_column,
            soft_delete_column=args.soft_delete_column,
        ) as scorer:
            report = scorer.score(sql, args.question)
    except (SqlgamiError, OSError) as exc:
        print(f"sqlgami: {exc}", file=sys.stderr)
        return 2
    print(report.to_json(indent=2) if args.json else report.summary())
    if args.fail_under is not None:
        value = report.confidence.value
        if value is None or value < args.fail_under:
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
