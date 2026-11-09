"""Check that a suite is internally sound before it is used to judge anything.

A suite is only useful as a yardstick if its own gold SQL is right, its planted
bugs really are wrong, and its "equivalent" rewrites really are equivalent.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import build
from .compare import Outcome, run_sql, same_outcome
from .suite import Question, Seed, Suite, Variant

BUG_CLASSES = {
    "fan_out", "distinct_masking", "missing_group_by", "outer_to_inner", "cartesian", "not_in_null",
    "eq_null", "limit_no_order", "avg_of_avg", "type_mismatch", "missing_tenant_filter",
    "date_off_by_one", "wrong_filter_value", "wrong_column", "wrong_join_key", "wrong_aggregate",
    "missing_condition", "extra_condition", "unknown_table", "unknown_column", "mutation",
    "syntax_error", "bq_no_partition_filter", "bq_unnest", "bq_struct_path", "bq_legacy_sql",
    "bq_current_date_utc", "cosmos_cross_container_join", "cosmos_missing_partition_key",
    "cosmos_array_vs_scalar", "cosmos_missing_field", "other",
}
OUTCOMES = {"error", "wrong_on_all_seeds", "wrong_on_nasty_only"}

ERROR = "error"
WARNING = "warning"


@dataclass
class Problem:
    level: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level.upper():7} {self.where}: {self.message}"


@dataclass
class Validation:
    suite: str
    engines: list[str]
    problems: list[Problem] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    skipped_engines: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(p.level == ERROR for p in self.problems)

    def add(self, level: str, where: str, message: str) -> None:
        self.problems.append(Problem(level, where, message))

    def summary(self) -> str:
        errors = sum(1 for p in self.problems if p.level == ERROR)
        warnings = len(self.problems) - errors
        lines = [
            f"suite {self.suite} on {', '.join(self.engines)}: "
            + ", ".join(f"{v} {k}" for k, v in self.counts.items())
            + f"; {errors} error(s), {warnings} warning(s)"
        ]
        for engine, why in self.skipped_engines.items():
            lines.append(f"skipped: {engine} ({why})")
        lines.extend(str(p) for p in self.problems)
        return "\n".join(lines)


class Runner:
    """Runs SQL against every seed of a suite, caching connections and gold results."""

    def __init__(self, suite: Suite, engines: list[str]) -> None:
        self.suite = suite
        self.engines = engines
        self._conns: dict[tuple[str, str], Any] = {}
        self._gold: dict[tuple[str, str, str], Outcome] = {}

    def conn(self, engine: str, seed: Seed) -> Any:
        key = (engine, seed.label)
        if key not in self._conns:
            self._conns[key] = build.connect(self.suite, engine, seed)
        return self._conns[key]

    def run(self, engine: str, seed: Seed, sql: str) -> Outcome:
        return run_sql(self.conn(engine, seed), sql)

    def gold(self, question: Question, engine: str, seed: Seed) -> Outcome | None:
        sql = question.gold(engine)
        if not sql:
            return None
        key = (question.id, engine, seed.label)
        if key not in self._gold:
            self._gold[key] = self.run(engine, seed, sql)
        return self._gold[key]

    def matches_gold(self, question: Question, engine: str, sql: str) -> dict[str, tuple[bool, str]]:
        """Per seed: whether ``sql`` returns what the gold SQL returns."""
        result: dict[str, tuple[bool, str]] = {}
        for seed in self.suite.seeds:
            gold = self.gold(question, engine, seed)
            if gold is None:
                continue
            outcome = self.run(engine, seed, sql)
            result[seed.label] = same_outcome(gold, outcome, question.ordered)
        return result

    def is_correct(self, question: Question, engine: str, sql: str) -> bool | None:
        """The test-suite label: correct only if it matches gold on every seed."""
        results = self.matches_gold(question, engine, sql)
        if not results:
            return None
        return all(equal for equal, _ in results.values())

    def close(self) -> None:
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()


def validate(
    suite: Suite,
    engines: list[str] | None = None,
    check_determinism: bool = True,
    strict: bool = False,
) -> Validation:
    wanted = [e for e in (engines or suite.engines) if e in suite.engines]
    engines = [e for e in wanted if e in build.SUPPORTED]
    result = Validation(suite.name, engines)
    result.skipped_engines = {e: build.why_unsupported(e) for e in wanted if e not in engines}
    result.counts = {
        "questions": len(suite.questions),
        "bugs": len(suite.bugs),
        "equivalents": len(suite.equivalents),
    }
    # A suite with nothing in it validates trivially, which says nothing about sqlgami.
    for kind, count in result.counts.items():
        if count == 0:
            result.add(ERROR if strict else WARNING, f"{kind}.jsonl", f"the suite has no {kind}")
    for engine, why in result.skipped_engines.items():
        if strict:
            result.add(ERROR, engine, f"declared in suite.yaml but not checked: {why}")

    profiles = [s.profile for s in suite.seeds]
    if profiles.count("clean") < 3:
        result.add(ERROR, "suite.yaml", "fewer than three clean seeds")
    if not suite.nasty_seeds():
        result.add(ERROR, "suite.yaml", "no nasty seed")

    if check_determinism:
        for seed in suite.seeds:
            first = build.generate(suite, seed)
            with tempfile.TemporaryDirectory() as tmp:
                second = build.generate(suite, seed, out=Path(tmp), force=True)
                if build.digest(first) != build.digest(second):
                    result.add(ERROR, f"generate.py seed {seed.seed}", "two runs gave different output")
            total = sum(build.row_counts(first).values())
            if total > 50_000:
                result.add(WARNING, f"generate.py seed {seed.seed}", f"{total} rows is above the 50,000 budget")

    for engine in engines:
        for seed in suite.seeds:
            try:
                build.build(suite, engine, seed)
            except Exception as exc:
                result.add(ERROR, f"{engine} {seed.label}", f"database does not build: {exc}")
                return result

    _check_ids(suite, result)
    runner = Runner(suite, engines)
    try:
        for question in suite.questions:
            _check_question(suite, runner, question, engines, result)
        for bug in suite.bugs:
            _check_variant(suite, runner, bug, engines, result)
        for equivalent in suite.equivalents:
            _check_variant(suite, runner, equivalent, engines, result)
    finally:
        runner.close()
    return result


def _check_ids(suite: Suite, result: Validation) -> None:
    seen: set[str] = set()
    for item in [*suite.questions, *suite.bugs, *suite.equivalents]:
        if item.id in seen:
            result.add(ERROR, item.id, "id is used more than once")
        seen.add(item.id)
    for variant in [*suite.bugs, *suite.equivalents]:
        if suite.question(variant.question_id) is None:
            result.add(ERROR, variant.id, f"refers to unknown question {variant.question_id}")
        if variant.engine not in suite.engines:
            result.add(ERROR, variant.id, f"engine {variant.engine} is not in the suite")
    for bug in suite.bugs:
        if bug.bug_class not in BUG_CLASSES:
            result.add(WARNING, bug.id, f"bug_class {bug.bug_class!r} is not in the agreed list")
        if bug.expected_outcome not in OUTCOMES:
            result.add(ERROR, bug.id, f"expected_outcome {bug.expected_outcome!r} is not recognised")


def _check_question(suite: Suite, runner: Runner, question: Question, engines: list[str], result: Validation) -> None:
    if question.answerable is False:
        if any(question.gold(e) for e in suite.engines):
            result.add(ERROR, question.id, "marked unanswerable but has gold SQL")
        return
    if not any(question.gold(e) for e in suite.engines):
        result.add(ERROR, question.id, "answerable but has no gold SQL for any engine")
        return
    shape = question.expected_answer_shape or {}
    allows_empty = False
    row_range = shape.get("row_count_range")
    if isinstance(row_range, (list, tuple)) and len(row_range) == 2 and row_range[0] == 0:
        allows_empty = True

    for engine in engines:
        sql = question.gold(engine)
        if not sql:
            continue
        lowered = sql.lower()
        for banned in ("current_date", "current_timestamp", "now()", "date('now'", "datetime('now'"):
            if banned in lowered:
                result.add(ERROR, f"{question.id} {engine}", f"gold SQL uses {banned}; results would change over time")
        non_empty_on_clean = False
        for seed in suite.seeds:
            outcome = runner.gold(question, engine, seed)
            if outcome is None:
                continue
            if not outcome.ok:
                result.add(ERROR, f"{question.id} {engine} {seed.label}", f"gold SQL fails: {outcome.error}")
                continue
            if outcome.rows and seed.profile == "clean":
                non_empty_on_clean = True
            columns = shape.get("columns")
            if isinstance(columns, list) and outcome.columns and len(columns) != len(outcome.columns):
                result.add(
                    WARNING,
                    f"{question.id} {engine}",
                    f"expected_answer_shape lists {len(columns)} columns, gold returns {len(outcome.columns)}",
                )
        if not non_empty_on_clean and not allows_empty:
            result.add(ERROR, f"{question.id} {engine}", "gold SQL returns no rows on any clean seed")

    present = [e for e in engines if question.gold(e)]
    for seed in suite.seeds:
        reference = runner.gold(question, present[0], seed) if present else None
        if reference is None or not reference.ok:
            continue
        for other in present[1:]:
            outcome = runner.gold(question, other, seed)
            if outcome is None or not outcome.ok:
                continue
            equal, why = same_outcome(reference, outcome, question.ordered)
            if not equal:
                result.add(
                    ERROR,
                    f"{question.id} {seed.label}",
                    f"gold differs between {present[0]} and {other}: {why}",
                )


def _check_variant(suite: Suite, runner: Runner, variant: Variant, engines: list[str], result: Validation) -> None:
    if variant.engine not in engines:
        return
    question = suite.question(variant.question_id)
    if question is None or not question.gold(variant.engine):
        if question is not None:
            result.add(ERROR, variant.id, f"question {question.id} has no gold SQL for {variant.engine}")
        return
    matches = runner.matches_gold(question, variant.engine, variant.sql)
    by_profile = {seed.label: seed.profile for seed in suite.seeds}
    clean = [equal for label, (equal, _) in matches.items() if by_profile[label] == "clean"]
    nasty = [equal for label, (equal, _) in matches.items() if by_profile[label] != "clean"]

    if variant.kind == "equivalent":
        for label, (equal, why) in matches.items():
            if not equal:
                result.add(ERROR, f"{variant.id} {label}", f"equivalent does not match gold: {why}")
        return

    if all(clean) and all(nasty):
        result.add(ERROR, variant.id, "planted bug returns the same as gold on every seed, so it is not a bug")
        return
    errors = []
    for seed in suite.seeds:
        outcome = runner.run(variant.engine, seed, variant.sql)
        errors.append(not outcome.ok)
    expected = variant.expected_outcome
    if expected == "error" and not all(errors):
        result.add(ERROR, variant.id, "expected to fail to run, but it runs on at least one seed")
    elif expected == "wrong_on_all_seeds" and (any(clean) or any(nasty)):
        result.add(ERROR, variant.id, "expected wrong on all seeds, but it matches gold on at least one")
    elif expected == "wrong_on_nasty_only" and (not all(clean) or any(nasty)):
        result.add(
            ERROR,
            variant.id,
            "expected to match gold on clean seeds and differ only on the nasty seed, but it does not",
        )
    if expected != "error" and all(errors):
        result.add(WARNING, variant.id, "fails to run on every seed; expected_outcome should be 'error'")
