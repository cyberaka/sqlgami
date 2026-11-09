"""Bench tooling.

    python -m benchkit build saas_billing
    python -m benchkit validate saas_billing
    python -m benchkit evaluate saas_billing --engine duckdb --mode sample
    python -m benchkit label saas_billing bench/.runs/<run-id>

A suite is named either by its directory under bench/ or by a path to it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import build as build_mod
from .evaluate import evaluate, format_metrics, label_run, read_candidates, write_run
from .suite import SuiteError, load_suite
from .validate import validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchkit", description="sqlgami bench tooling")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="generate data and build the databases")
    p_build.add_argument("suite")
    p_build.add_argument("--engine", action="append", help="repeatable; default: every supported engine")
    p_build.add_argument("--force", action="store_true")

    p_validate = sub.add_parser("validate", help="check the suite's gold, bugs and equivalents")
    p_validate.add_argument("suite")
    p_validate.add_argument("--engine", action="append")

    p_eval = sub.add_parser("evaluate", help="score the suite with sqlgami and print metrics")
    p_eval.add_argument("suite")
    p_eval.add_argument("--engine", action="append")
    p_eval.add_argument("--mode", action="append", help="static, dry or sample; repeatable")
    p_eval.add_argument("--seed", help="seed label to score against, e.g. nasty-42")
    p_eval.add_argument("--candidates", help="path to a candidates.jsonl file")
    p_eval.add_argument("--run-id", help="name of the directory under bench/.runs/")

    p_label = sub.add_parser("label", help="label candidate SQL as right or wrong by running it on every seed")
    p_label.add_argument("suite")
    p_label.add_argument("run", help="a run directory holding candidates.jsonl, or the file itself")

    args = parser.parse_args(argv)
    try:
        suite = load_suite(args.suite)
        if args.command == "label":
            target = label_run(suite, Path(args.run))
            labels = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
            right = sum(1 for row in labels if row["correct"] is True)
            wrong = sum(1 for row in labels if row["correct"] is False)
            print(f"{len(labels)} candidates: {right} correct, {wrong} wrong, {len(labels) - right - wrong} unlabelled")
            print(f"labels: {target}")
            return 0
        engines = [e for e in (args.engine or suite.engines) if e in build_mod.SUPPORTED]
        if not engines:
            print("no engine benchkit can build was selected", file=sys.stderr)
            return 2

        if args.command == "build":
            for engine in engines:
                for seed in suite.seeds:
                    path = build_mod.build(suite, engine, seed, force=args.force)
                    print(f"{engine} {seed.label}: {path}")
            return 0

        if args.command == "validate":
            result = validate(suite, engines)
            print(result.summary())
            return 0 if result.ok else 1

        candidates = read_candidates(Path(args.candidates)) if args.candidates else None
        run_id = args.run_id or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        for engine in engines:
            for mode in args.mode or ["static", "dry", "sample"]:
                items, result = evaluate(suite, engine, mode, args.seed, candidates)
                path = write_run(suite, engine, mode, items, result, run_id)
                print(f"== {suite.name} / {engine} / {mode}")
                print(format_metrics(result))
                print(f"details: {path}\n")
        return 0
    except SuiteError as exc:
        print(f"benchkit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
