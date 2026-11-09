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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
