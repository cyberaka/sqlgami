"""Audit gold against strong candidates: where two capable generators agree with each other
and differ from gold, the gold (or the wording) is the suspect.

This is how the Batch 3 and 3b gold errors were found. It is a check on the suite, not on
sqlgami, so it runs with ``benchkit audit`` and inside ``benchkit validate --candidates``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .compare import same_outcome
from .suite import Question, Suite
from .validate import Runner

DEFAULT_STRONG = ("claude",)  # generators whose name contains one of these are "strong"


@dataclass
class Suspect:
    question_id: str
    engine: str
    generators: list[str]  # strong generators that agree with each other
    seeds_differing: list[str]  # seeds where that shared answer differs from gold
    why: str
    others_agreeing: list[str] = field(default_factory=list)  # weaker generators on the same answer

    def __str__(self) -> str:
        return (
            f"{self.question_id} {self.engine}: {len(self.generators)} strong generators agree with each other "
            f"({', '.join(self.generators)}) and differ from gold on {', '.join(self.seeds_differing)}: {self.why}"
            + (f"; also {', '.join(self.others_agreeing)}" if self.others_agreeing else "")
        )


def _is_strong(generator: str, strong: tuple[str, ...]) -> bool:
    name = generator.lower()
    return any(marker.lower() in name for marker in strong)


def audit(
    suite: Suite,
    candidate_files: list[Path],
    engines: list[str] | None = None,
    strong: tuple[str, ...] = DEFAULT_STRONG,
    min_agreeing: int = 2,
) -> list[Suspect]:
    """Questions whose gold disagrees with at least ``min_agreeing`` strong generators that agree among themselves."""
    rows: list[dict[str, Any]] = []
    for path in candidate_files:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    chosen = [e for e in (engines or suite.engines)]
    runner = Runner(suite, chosen)
    suspects: list[Suspect] = []
    try:
        for question in suite.questions:
            for engine in chosen:
                if not question.gold(engine):
                    continue
                mine = [
                    r for r in rows
                    if r.get("question_id") == question.id and r.get("engine") == engine and r.get("sql")
                    and int(r.get("sample") or 0) == 0
                ]
                found = _audit_question(suite, runner, question, engine, mine, strong, min_agreeing)
                if found is not None:
                    suspects.append(found)
    finally:
        runner.close()
    return suspects


def _audit_question(
    suite: Suite,
    runner: Runner,
    question: Question,
    engine: str,
    rows: list[dict[str, Any]],
    strong: tuple[str, ...],
    min_agreeing: int,
) -> Suspect | None:
    # Results per candidate per seed, only for candidates that run everywhere.
    results: dict[str, dict[str, Any]] = {}
    for row in rows:
        generator = row.get("generator") or "?"
        per_seed = {}
        for seed in suite.seeds:
            outcome = runner.run(engine, seed, question.render(row["sql"]))
            if not outcome.ok:
                per_seed = None
                break
            per_seed[seed.label] = outcome
        if per_seed:
            results[generator] = per_seed
    strong_ones = [g for g in results if _is_strong(g, strong)]
    if len(strong_ones) < min_agreeing:
        return None

    def agree(a: str, b: str) -> bool:
        return all(same_outcome(results[a][s.label], results[b][s.label], question.ordered)[0] for s in suite.seeds)

    # Groups of strong generators that agree with one another on every seed.
    groups: list[list[str]] = []
    for generator in strong_ones:
        for group in groups:
            if agree(group[0], generator):
                group.append(generator)
                break
        else:
            groups.append([generator])
    for group in sorted(groups, key=len, reverse=True):
        if len(group) < min_agreeing:
            continue
        leader = group[0]
        differing, why = [], ""
        for seed in suite.seeds:
            gold = runner.gold(question, engine, seed)
            if gold is None or not gold.ok:
                continue
            equal, reason = same_outcome(gold, results[leader][seed.label], question.ordered)
            if not equal:
                differing.append(seed.label)
                why = why or reason
        if differing:
            others = [g for g in results if g not in strong_ones and agree(leader, g)]
            return Suspect(question.id, engine, group, differing, why, others)
    return None
