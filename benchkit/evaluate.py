"""Score every labelled SQL in a suite with sqlgami and measure how well the scores track truth.

Labels come from execution, never from trust: a statement is correct only if
it returns what the gold SQL returns on every seed, the nasty one included.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlgami import Scorer

from . import build
from .suite import RUNS_DIR, Question, Suite
from .validate import Runner


@dataclass
class Item:
    id: str
    question_id: str
    kind: str  # gold | equivalent | bug | candidate
    sql: str
    correct: bool
    runs: bool = True
    bug_class: str | None = None
    expected_outcome: str | None = None
    score: float | None = None
    verdict: str = ""
    findings: list[str] = field(default_factory=list)
    high_findings: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    difficulty: str = ""
    generator: str = ""
    question: str = ""
    cap_reason: str = ""
    failed_stage: str = ""
    agreeing: int | None = None  # consistency stage: alternatives that returned the same result
    comparable: int | None = None


def auroc(scores: list[float], labels: list[bool]) -> float | None:
    """Probability that a correct item outscores a wrong one (ties count half)."""
    positives = [s for s, y in zip(scores, labels) if y]
    negatives = [s for s, y in zip(scores, labels) if not y]
    if not positives or not negatives:
        return None
    ranked = sorted(zip(scores, labels))
    rank_sum = 0.0
    i = 0
    while i < len(ranked):
        j = i
        while j < len(ranked) and ranked[j][0] == ranked[i][0]:
            j += 1
        average_rank = (i + 1 + j) / 2
        rank_sum += average_rank * sum(1 for k in range(i, j) if ranked[k][1])
        i = j
    n_pos, n_neg = len(positives), len(negatives)
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def expected_calibration_error(scores: list[float], labels: list[bool], bins: int = 10) -> float | None:
    if not scores:
        return None
    total = len(scores)
    error = 0.0
    for b in range(bins):
        low, high = b / bins, (b + 1) / bins
        members = [(s, y) for s, y in zip(scores, labels) if (low <= s < high) or (b == bins - 1 and s == 1.0)]
        if not members:
            continue
        confidence = sum(s for s, _ in members) / len(members)
        hit_rate = sum(1 for _, y in members if y) / len(members)
        error += len(members) / total * abs(confidence - hit_rate)
    return error


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def metrics(items: list[Item]) -> dict[str, Any]:
    scored = [i for i in items if i.score is not None]
    scores = [i.score for i in scored]
    labels = [i.correct for i in scored]
    correct = [i for i in scored if i.correct]
    wrong = [i for i in scored if not i.correct]
    silent = [i for i in wrong if i.runs]  # runs fine but returns the wrong data

    def share(part: list[Item], whole: list[Item]) -> float | None:
        return len(part) / len(whole) if whole else None

    at_90 = [i for i in scored if i.score >= 0.9]
    at_70 = [i for i in scored if i.score >= 0.7]
    by_class: dict[str, dict[str, Any]] = {}
    for item in wrong:
        key = item.bug_class or item.kind
        entry = by_class.setdefault(key, {"total": 0, "flagged": 0, "runs": 0})
        entry["total"] += 1
        entry["runs"] += int(item.runs)
        entry["flagged"] += int(item.score <= 0.4)
    for entry in by_class.values():
        entry["flagged_share"] = round(entry["flagged"] / entry["total"], 3)

    at_80 = [i for i in scored if i.score >= 0.8]
    by_difficulty: dict[str, dict[str, Any]] = {}
    for item in scored:
        entry = by_difficulty.setdefault(item.difficulty or "unknown", {"correct": 0, "correct_flagged": 0, "wrong": 0, "wrong_flagged": 0})
        if item.correct:
            entry["correct"] += 1
            entry["correct_flagged"] += int(item.score <= 0.4)
        else:
            entry["wrong"] += 1
            entry["wrong_flagged"] += int(item.score <= 0.4)
    by_kind: dict[str, dict[str, Any]] = {}
    for item in scored:
        entry = by_kind.setdefault(item.kind, {"n": 0, "correct": 0, "scores": []})
        entry["n"] += 1
        entry["correct"] += int(item.correct)
        entry["scores"].append(item.score)
    for entry in by_kind.values():
        values = sorted(entry.pop("scores"))
        entry["score_min"] = round(values[0], 3)
        entry["score_median"] = round(values[len(values) // 2], 3)
        entry["score_max"] = round(values[-1], 3)
        entry["at_or_below_0.4"] = sum(1 for v in values if v <= 0.4)
        entry["at_or_above_0.7"] = sum(1 for v in values if v >= 0.7)
    by_generator: dict[str, dict[str, Any]] = {}
    for item in scored:
        if item.kind != "candidate":
            continue
        entry = by_generator.setdefault(item.generator or "unknown", {"n": 0, "correct": 0})
        entry["n"] += 1
        entry["correct"] += int(item.correct)
    for entry in by_generator.values():
        entry["accuracy"] = round(entry["correct"] / entry["n"], 3)

    latencies = [i.latency_ms for i in scored]
    return {
        "precision_at_0.8": share([i for i in at_80 if i.correct], at_80),
        "count_at_0.8": len(at_80),
        "by_difficulty": dict(sorted(by_difficulty.items())),
        "by_kind": dict(sorted(by_kind.items())),
        "by_generator": dict(sorted(by_generator.items())),
        "items": len(scored),
        "correct": len(correct),
        "wrong": len(wrong),
        "auroc": auroc(scores, labels),
        "ece": expected_calibration_error(scores, labels),
        "precision_at_0.9": share([i for i in at_90 if i.correct], at_90),
        "count_at_0.9": len(at_90),
        "precision_at_0.7": share([i for i in at_70 if i.correct], at_70),
        "count_at_0.7": len(at_70),
        "silent_wrong": len(silent),
        "silent_wrong_flagged_share": share([i for i in silent if i.score <= 0.4], silent),
        "silent_wrong_below_half_share": share([i for i in silent if i.score < 0.5], silent),
        "correct_flagged_share": share([i for i in correct if i.score <= 0.4], correct),
        "mean_score_correct": statistics.fmean(i.score for i in correct) if correct else None,
        "mean_score_wrong": statistics.fmean(i.score for i in wrong) if wrong else None,
        "latency_ms_p50": _percentile(latencies, 50),
        "latency_ms_p95": _percentile(latencies, 95),
        "by_bug_class": dict(sorted(by_class.items())),
    }


def read_candidates(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def evaluate(
    suite: Suite,
    engine: str,
    mode: str = "sample",
    seed_label: str | None = None,
    candidates: list[dict[str, Any]] | None = None,
    include_planted: bool = True,
    consistency: bool = False,
    verifiers: list[str] | None = None,
    max_samples: int | None = None,
    only_generators: list[str] | None = None,
) -> tuple[list[Item], dict[str, Any]]:
    """Score every labelled statement for one engine. Returns the items and their metrics.

    With ``consistency``, each candidate is also compared with the other generators'
    candidates for the same question, standing in for alternatives a host would
    generate. ``verifiers`` limits which generators may act as alternatives, to
    measure a realistic setup (weaker models checking stronger ones, or the reverse).
    """
    seed = next((s for s in suite.seeds if s.label == seed_label), None) if seed_label else None
    if seed is None:
        seed = (suite.nasty_seeds() or suite.seeds)[-1]
    runner = Runner(suite, [engine])
    conn = build.connect(suite, engine, seed)
    scorers: dict[bool, Scorer] = {}
    glossary_file = suite.path / "glossary.yaml"
    glossary = glossary_file.read_text(encoding="utf-8") if glossary_file.exists() else None

    def scorer_for(question: Question) -> Scorer:
        scoped = question.tenant is not None
        if scoped not in scorers:
            scorers[scoped] = Scorer(
                engine,
                db=conn,
                tenant_column=suite.tenant_column if scoped else None,
                soft_delete_column=suite.soft_delete_column,
                soft_delete_policy=suite.soft_delete_policy,
                glossary=glossary,
            )
        return scorers[scoped]

    items: list[Item] = []

    by_question: dict[str, list[dict[str, Any]]] = {}
    for row in candidates or []:
        if row.get("engine") == engine and row.get("sql"):
            by_question.setdefault(row.get("question_id", ""), []).append(row)

    def alternatives_for(question: Question, generator: str) -> list[tuple[str, str]] | None:
        if not consistency:
            return None
        return [
            (row["sql"], row.get("generator") or "")  # tagged by model: its samples are one voice
            for row in by_question.get(question.id, [])
            if row.get("generator") != generator
            and (not verifiers or row.get("generator") in verifiers)
            and (max_samples is None or int(row.get("sample") or 0) < max_samples)
        ]

    def add(item_id: str, question: Question, kind: str, sql: str, generator: str = "", **extra: Any) -> None:
        if not question.gold(engine):
            return
        correct = True if kind == "gold" else runner.is_correct(question, engine, sql)
        if correct is None:
            return
        runs = runner.run(engine, seed, question.render(sql)).ok
        item = Item(item_id, question.id, kind, sql, bool(correct), runs, **extra)
        started = time.perf_counter()
        # sqlgami gets the statement as written (a template stays a template) plus its values.
        report = scorer_for(question).score(
            sql,
            question.question,
            mode=mode,
            params=question.param_values() or None,
            alternatives=alternatives_for(question, generator) if kind == "candidate" else None,
        )
        item.latency_ms = (time.perf_counter() - started) * 1000
        item.score = report.score
        item.verdict = report.verdict.value
        item.findings = [f.code for f in report.findings if f.severity.value != "info"]
        item.high_findings = [f.code for f in report.findings if f.severity.value in ("high", "error")]
        item.difficulty = question.difficulty
        item.question = question.question
        item.cap_reason = report.confidence.cap_reason or ""
        item.failed_stage = next((st.name for st in report.stages if st.status.value == "failed"), "")
        item.generator = generator
        signal = report.signals.get("consistency") or {}
        item.agreeing = signal.get("agreeing_sources")
        item.comparable = signal.get("sources")
        items.append(item)

    try:
        for question in suite.questions:
            if question.gold(engine):
                add(f"{question.id}:gold", question, "gold", question.gold(engine))
        if include_planted:
            for variant in [*suite.equivalents, *suite.bugs]:
                if variant.engine != engine:
                    continue
                question = suite.question(variant.question_id)
                if question is None:
                    continue
                add(
                    variant.id,
                    question,
                    variant.kind,
                    variant.sql,
                    bug_class=variant.bug_class,
                    expected_outcome=variant.expected_outcome,
                )
        for index, row in enumerate(candidates or []):
            if row.get("engine") != engine:
                continue
            question = suite.question(row.get("question_id", ""))
            if question is None or not row.get("sql"):
                continue
            if only_generators and row.get("generator") not in only_generators:
                continue
            add(
                f"cand-{index}:{row.get('generator', '')}:{row.get('sample', 0)}",
                question,
                "candidate",
                row["sql"],
                generator=row.get("generator", ""),
            )
    finally:
        for scorer in scorers.values():
            scorer.close()
        runner.close()
        conn.close()
    return items, metrics(items)


def label_run(suite: Suite, run: Path) -> Path:
    """Label every candidate in a run directory (or a candidates.jsonl file) by execution.

    Writes ``labels.jsonl`` beside the candidates: one line per candidate, in the
    same order, with ``correct`` true only if it matches gold on every seed.
    ``correct`` is null where no label can be given (no gold, unknown question).
    """
    source = run / "candidates.jsonl" if run.is_dir() else run
    rows = read_candidates(source)
    engines = sorted({r.get("engine") for r in rows if r.get("engine") in build.SUPPORTED})
    runner = Runner(suite, engines)
    out_rows = []
    try:
        for row in rows:
            entry = {
                "question_id": row.get("question_id"),
                "engine": row.get("engine"),
                "generator": row.get("generator"),
                "sample": row.get("sample"),
                "sql": row.get("sql"),
                "correct": None,
                "runs": None,
                "per_seed": {},
                "matched": None,
                "note": "",
            }
            question = suite.question(row.get("question_id") or "")
            engine = row.get("engine")
            if question is None:
                entry["note"] = "unknown question"
            elif engine not in engines:
                entry["note"] = f"engine {engine!r} cannot be built here"
            elif not question.gold(engine):
                entry["note"] = "no gold SQL for this engine"
            elif not row.get("sql"):
                entry["note"] = "no SQL"
            else:
                matches = runner.matches_gold(question, engine, row["sql"])
                entry["per_seed"] = {label: equal for label, (equal, _) in matches.items()}
                matched = runner.matched_answer(question, engine, row["sql"])
                entry["correct"] = bool(matched)
                entry["matched"] = matched or None  # "gold", or which accepted alternative
                entry["runs"] = all(runner.run(engine, seed, question.render(row["sql"])).ok for seed in suite.seeds)
                reasons = [f"{label}: {why}" for label, (equal, why) in matches.items() if not equal]
                entry["note"] = "; ".join(reasons[:2])
            out_rows.append(entry)
    finally:
        runner.close()
    target = source.parent / "labels.jsonl"
    target.write_text("".join(json.dumps(r) + "\n" for r in out_rows), encoding="utf-8")
    return target


def calibration_samples(items: list[Item], depth: str) -> list[tuple[float, bool, str, str]]:
    """``(score, correct, depth, question id)`` for items that got past the hard gates.

    Gated statements keep their fixed near-zero score and are not part of the fit.
    """
    return [(i.score, i.correct, depth, i.question_id) for i in items if i.score is not None and i.score > 0.1]


def fit_calibration(samples: list[tuple[float, bool, str, str]], folds: int = 5, meta: dict[str, Any] | None = None):
    """Fit a calibrator and estimate its error on questions it was not fitted on.

    Folds are split by question, so a question's gold, bugs and rewrites never
    sit on both sides of a split.
    """
    from sqlgami import IsotonicCalibrator

    calibrator = IsotonicCalibrator.fit([(s, y, d) for s, y, d, _ in samples], meta=meta)
    questions = sorted({q for _, _, _, q in samples})
    held_scores: list[float] = []
    held_labels: list[bool] = []
    raw_scores: list[float] = []
    if len(questions) >= folds:
        for fold in range(folds):
            held = {q for index, q in enumerate(questions) if index % folds == fold}
            train = [(s, y, d) for s, y, d, q in samples if q not in held]
            part = IsotonicCalibrator.fit(train, min_samples=max(10, 30 * (folds - 1) // folds))
            for score, correct, depth, question in samples:
                if question not in held:
                    continue
                value = part.calibrate(score, depth)
                if value is None:
                    continue
                held_scores.append(value)
                held_labels.append(correct)
                raw_scores.append(score)
    report = {
        "samples": len(samples),
        "questions": len(questions),
        "fitted_depths": sorted(calibrator.curves),
        "held_out_items": len(held_scores),
        "ece_before": expected_calibration_error(raw_scores, held_labels),
        "ece_after_held_out": expected_calibration_error(held_scores, held_labels),
        "auroc_before": auroc(raw_scores, held_labels),
        "auroc_after_held_out": auroc(held_scores, held_labels),
    }
    calibrator.meta["cross_validation"] = report
    return calibrator, report


def write_run(suite: Suite, engine: str, mode: str, items: list[Item], result: dict[str, Any], run_id: str) -> Path:
    out = RUNS_DIR / run_id
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"eval-{suite.name}-{engine}-{mode}.json"
    path.write_text(
        json.dumps({"suite": suite.name, "engine": engine, "mode": mode, "metrics": result,
                    "items": [asdict(i) for i in items]}, indent=2),
        encoding="utf-8",
    )
    return path


def format_metrics(result: dict[str, Any]) -> str:
    def pct(value: Any) -> str:
        return "n/a" if value is None else f"{value * 100:.1f}%"

    def num(value: Any, digits: int = 3) -> str:
        return "n/a" if value is None else f"{value:.{digits}f}"

    lines = [
        f"items {result['items']} (correct {result['correct']}, wrong {result['wrong']})",
        f"AUROC {num(result['auroc'])}   ECE {num(result['ece'])}",
        f"precision at score >= 0.9: {pct(result['precision_at_0.9'])} of {result['count_at_0.9']}",
        f"precision at score >= 0.7: {pct(result['precision_at_0.7'])} of {result['count_at_0.7']}",
        f"runs-but-wrong flagged (score <= 0.4): {pct(result['silent_wrong_flagged_share'])} of {result['silent_wrong']}",
        f"runs-but-wrong below 0.5: {pct(result['silent_wrong_below_half_share'])}",
        f"correct SQL wrongly flagged (score <= 0.4): {pct(result['correct_flagged_share'])}",
        f"mean score: correct {num(result['mean_score_correct'], 2)}, wrong {num(result['mean_score_wrong'], 2)}",
        f"latency p50 {num(result['latency_ms_p50'], 1)} ms, p95 {num(result['latency_ms_p95'], 1)} ms",
    ]
    lines.append(f"precision at score >= 0.8: {pct(result['precision_at_0.8'])} of {result['count_at_0.8']}")
    lines.append("by kind (n, correct, score min/median/max, <=0.4, >=0.7):")
    for name, entry in result["by_kind"].items():
        lines.append(
            f"  {name:12} n={entry['n']:3} correct={entry['correct']:3} "
            f"{entry['score_min']:.2f}/{entry['score_median']:.2f}/{entry['score_max']:.2f} "
            f"low={entry['at_or_below_0.4']:3} high={entry['at_or_above_0.7']:3}"
        )
    lines.append("by difficulty (wrong flagged / wrong, correct flagged / correct):")
    for name, entry in result["by_difficulty"].items():
        lines.append(
            f"  {name:10} {entry['wrong_flagged']:3} / {entry['wrong']:3}    {entry['correct_flagged']:3} / {entry['correct']:3}"
        )
    for name, entry in result["by_generator"].items():
        lines.append(f"generator {name}: {entry['correct']} of {entry['n']} correct ({entry['accuracy'] * 100:.0f}%)")
    if result["by_bug_class"]:
        lines.append("by bug class (flagged / total):")
        for name, entry in result["by_bug_class"].items():
            lines.append(f"  {name:28} {entry['flagged']:3} / {entry['total']:3}")
    return "\n".join(lines)
