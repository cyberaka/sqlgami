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
from .evaluate import (
    calibration_samples,
    evaluate,
    fit_calibration,
    format_metrics,
    label_run,
    read_candidates,
    write_run,
)
from .audit import DEFAULT_STRONG, audit
from .suite import SuiteError, load_suite, RUNS_DIR
from .validate import validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchkit", description="sqlgami bench tooling")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="copy a suite from its shared or held-out source and record the checksums")
    p_sync.add_argument("source")
    p_sync.add_argument("dest")

    p_comp = sub.add_parser("compile", help="compile the suite into a bundle: fixtures plus the golds as references")
    p_comp.add_argument("suite")
    p_comp.add_argument("--engine", default="duckdb")
    p_comp.add_argument("--seed", type=int, default=1)
    p_comp.add_argument("--profiles", help="comma-separated fixture profiles")
    p_comp.add_argument("--from-seeds", action="store_true", help="subsample the suite's nasty seed databases instead of generating rows")
    p_comp.add_argument("--rules", help="YAML with a subsample: block of per-table predicates (with --from-seeds)")
    p_comp.add_argument("--references", help="JSONL of host-registered references (id, question, sql, n_default) replacing golds of the same id")
    p_comp.add_argument("--out", help="bundle folder (default bench/.runs/bundles/<suite>-<engine>)")
    p_comp.add_argument("--expand", help="offline expansion with a model: hf:<model> or claude:<model> (phrases to review, variants into the bundle)")
    p_comp.add_argument("--per-term", type=int, default=6, help="wordings to ask for per term (with --expand)")
    p_comp.add_argument("--per-example", type=int, default=4, help="paraphrases to ask for per reference (with --expand)")
    p_comp.add_argument("--accept", action="store_true", help="with --expand: write the accepted wordings into the bundle's Terms sheet copy (a tuned measurement)")

    p_lat = sub.add_parser("latency", help="p50/p90/p99 of score() per mode on the suite's gold statements")
    p_lat.add_argument("suite")
    p_lat.add_argument("--engine", default="duckdb")
    p_lat.add_argument("--mode", action="append", help="static, dry or sample; repeatable; default static and sample")
    p_lat.add_argument("--repeat", type=int, default=3)
    p_lat.add_argument("--stretch", help="pad statements to this line range, e.g. 40-80")

    p_build = sub.add_parser("build", help="generate data and build the databases")
    p_build.add_argument("suite")
    p_build.add_argument("--engine", action="append", help="repeatable; default: every supported engine")
    p_build.add_argument("--force", action="store_true")

    p_validate = sub.add_parser("validate", help="check the suite's gold, bugs and equivalents")
    p_validate.add_argument("suite")
    p_validate.add_argument("--engine", action="append")
    p_validate.add_argument(
        "--strict",
        action="store_true",
        help="fail on an empty suite and on engines the suite declares but this machine cannot check",
    )
    p_validate.add_argument(
        "--candidates",
        action="append",
        help="candidate files to audit the gold against (repeatable); see the audit command",
    )
    p_validate.add_argument("--strong", action="append", help="with --candidates: name markers of strong generators")

    p_audit = sub.add_parser("audit", help="find gold that strong generators agree against")
    p_audit.add_argument("suite")
    p_audit.add_argument("candidates", nargs="+", help="candidate files (jsonl)")
    p_audit.add_argument("--engine", action="append")
    p_audit.add_argument(
        "--strong",
        action="append",
        help="a marker naming a strong generator, matched inside the generator name (repeatable; default: claude)",
    )
    p_audit.add_argument("--min-agreeing", type=int, default=2, help="strong generators that must agree (default 2)")

    p_eval = sub.add_parser("evaluate", help="score the suite with sqlgami and print metrics")
    p_eval.add_argument("suite")
    p_eval.add_argument("--engine", action="append")
    p_eval.add_argument("--mode", action="append", help="static, dry or sample; repeatable")
    p_eval.add_argument("--seed", help="seed label to score against, e.g. nasty-42")
    p_eval.add_argument("--candidates", help="path to a candidates.jsonl file")
    p_eval.add_argument("--run-id", help="name of the directory under bench/.runs/")
    p_eval.add_argument(
        "--consistency",
        action="store_true",
        help="compare each candidate with the other generators' candidates for the same question",
    )
    p_eval.add_argument(
        "--verifiers",
        action="append",
        help="with --consistency: only these generators act as alternatives (repeatable)",
    )

    p_cal = sub.add_parser("calibrate", help="fit a calibration file from the suite's labelled SQL")
    p_cal.add_argument("suite", nargs="+", help="one or more suites to fit on together")
    p_cal.add_argument("--engine", action="append")
    p_cal.add_argument("--out", required=True, help="where to write the calibration JSON")

    p_eval.add_argument("--no-terms", action="store_true", help="score without the suite's term catalogue (terms/ folder), to report both numbers")
    p_eval.add_argument("--bundle", nargs="?", const="auto", help="score with a compiled bundle (default: the suite's own, from benchkit compile)")
    p_eval.add_argument("--terms-from-bundle", action="store_true", help="with --bundle: match terms with the bundle's Terms copy (after --accept) instead of the suite's")
    p_eval.add_argument("--role", choices=["confirm", "judge"], default="confirm", help="how far the alternatives are trusted")
    p_eval.add_argument("--max-samples", type=int, help="with --consistency: use only samples 0..N-1 of each verifier")
    p_eval.add_argument("--score-only", action="append", help="score only candidates from these generators (repeatable)")
    p_eval.add_argument(
        "--substitute",
        action="store_true",
        help="score the SQL with its template parameters filled in, instead of the template plus values",
    )

    p_label = sub.add_parser("label", help="label candidate SQL as right or wrong by running it on every seed")
    p_label.add_argument("suite")
    p_label.add_argument("run", help="a run directory holding candidates.jsonl, or the file itself")

    args = parser.parse_args(argv)
    try:
        if args.command == "calibrate":
            samples = []
            names = []
            for name in args.suite:
                one = load_suite(name)
                names.append(one.name)
                for engine in [e for e in (args.engine or one.engines) if e in build_mod.SUPPORTED]:
                    for mode in ("static", "dry", "sample"):
                        items, _ = evaluate(one, engine, mode)
                        samples.extend(calibration_samples(items, mode))
            calibrator, report = fit_calibration(samples, meta={"suites": names})
            calibrator.save(args.out)
            print(json.dumps(report, indent=2))
            print(f"calibration: {args.out}")
            return 0
        if args.command == "sync":
            from .suite import sync_suite

            recorded = sync_suite(args.source, args.dest)
            for name, digest in recorded.items():
                print(f"{digest}  {name}")
            return 0
        suite = load_suite(args.suite)
        if args.command == "compile":
            from .compile import compile_suite

            bundle = compile_suite(
                suite, args.engine, args.seed, args.profiles.split(",") if args.profiles else None,
                from_seeds=args.from_seeds, rules_file=Path(args.rules) if args.rules else None,
                references_file=Path(args.references) if args.references else None, out=Path(args.out) if args.out else None,
                expand=args.expand, per_term=args.per_term, per_example=args.per_example, accept=args.accept,
            )
            if m := bundle.manifest.get("expansion"):
                print(f"  expansion ({m['model']}): {m['proposals']} proposals, {len(m['phrases'])} wordings accepted for review, {len(m['variants'])} variants compiled, {m['rejected']} rejected" + (" — wordings accepted into the bundle's Terms copy" if bundle.manifest.get("expansion_accepted") else ""))
            m = bundle.manifest
            from sqlgami.bundle import bundle_hash

            print(f"bundle: {bundle.path}  references {m['references']} (rejected {m['rejected']})  hash {bundle_hash(bundle.path)[:16]}")
            for entry in m["fixtures"]:
                print(f"  {entry['name']:16} {entry['rows']:6} rows   gold p50 {entry['reference_ms'] or 0:.1f} ms  max {entry['reference_max_ms'] or 0:.1f} ms   bugs diverging {entry.get('discrimination', '-')}/{m.get('bugs_measured', '-')}")
            for key, entries in (m.get("references_with_free_numbers") or {}).items():
                said = "; ".join(f"{e['number']:g} in \"{e['phrase']}\"" for e in entries)
                print(f"  note: reference {key} states {said} and no slot binds it: only a question stating the same number compares under it — reword without the number, or make it a {{slot}} of a term")
            return 0
        if args.command == "latency":
            from .latency import format_result, measure, write_result

            lines = None
            if args.stretch:
                low, _, high = args.stretch.partition("-")
                lines = (int(low), int(high or low))
            result = measure(suite, args.engine, args.mode or ["static", "sample"], repeat=args.repeat, lines=lines)
            print(format_result(result))
            write_result(result, RUNS_DIR / "latency.json")
            return 0
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

        if args.command == "audit":
            strong = tuple(args.strong) if args.strong else DEFAULT_STRONG
            suspects = audit(suite, [Path(p) for p in args.candidates], engines, strong, args.min_agreeing)
            for suspect in suspects:
                print(f"SUSPECT {suspect}")
            print(f"{len(suspects)} question(s) where strong generators agree against gold")
            return 1 if suspects else 0

        if args.command == "validate":
            result = validate(suite, args.engine, strict=args.strict)
            print(result.summary())
            if args.candidates:
                strong = tuple(args.strong) if args.strong else DEFAULT_STRONG
                suspects = audit(suite, [Path(p) for p in args.candidates], engines, strong)
                for suspect in suspects:
                    print(f"SUSPECT {suspect}")
                print(f"audit: {len(suspects)} question(s) where strong generators agree against gold")
                return 0 if result.ok and not suspects else 1
            return 0 if result.ok else 1

        candidates = read_candidates(Path(args.candidates)) if args.candidates else None
        run_id = args.run_id or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        for engine in engines:
            for mode in args.mode or ["static", "dry", "sample"]:
                items, result = evaluate(
                    suite,
                    engine,
                    mode,
                    args.seed,
                    candidates,
                    consistency=args.consistency,
                    verifiers=args.verifiers,
                    max_samples=args.max_samples,
                    only_generators=args.score_only,
                    role=args.role,
                    substitute=args.substitute,
                    terms=not args.no_terms,
                    bundle=args.bundle,
                    terms_from_bundle=args.terms_from_bundle,
                )
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
