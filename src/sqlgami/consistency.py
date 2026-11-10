"""Execution agreement: do independently written queries for the same question return the same data?

Static checks judge a query by its shape. This stage judges it by its answer.
The host supplies other attempts at the same question (or a function that
writes them); each is run read-only under the same limits as the candidate,
and the result sets are compared.

* The candidate matches most of the alternatives: strong support.
* The alternatives match one another but not the candidate: strong doubt.
* Nothing matches anything: the question is probably ambiguous.

How much this is worth depends on how independent and how capable the
alternatives are. Alternatives from the same model and prompt as the candidate
tend to share its mistakes.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Any, Callable, Iterable, Sequence

from sqlglot import exp

from . import analysis as A
from . import safety
from .catalog import Catalog
from .engines.base import Adapter
from .errors import SqlgamiError
from .report import Finding, Severity

logger = logging.getLogger("sqlgami")

# (question, schema description, glossary or None, how many) -> SQL statements
Generator = Callable[[str, str, "str | None", int], Sequence[str]]

AGREES = "agrees"
AGREES_REORDERED = "agrees_columns_reordered"
AGREES_OTHER_ORDER = "agrees_rows_in_another_order"  # both say ORDER BY, and the orders differ
BOTH_EMPTY = "both_empty"  # no rows, or nothing but NULL, on both sides: evidence of nothing
DIFFERS = "differs"
INVALID = "invalid"  # not a runnable read-only query against this schema
FAILED = "failed"  # the engine could not run it
SKIPPED = "skipped"  # too costly, or its result was cut off


@dataclass
class Alternative:
    sql: str
    status: str
    rows: int | None = None
    detail: str | None = None
    cluster: int | None = None
    source: str | None = None  # who wrote it: a model name, a prompt variant...

    def to_dict(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "source": self.source,
            "status": self.status,
            "rows": self.rows,
            "detail": self.detail,
            "cluster": self.cluster,
        }


@dataclass
class Outcome:
    ran: bool = False
    reason: str | None = None
    alternatives: list[Alternative] = field(default_factory=list)
    comparable: int = 0  # alternatives whose results could be compared
    agreeing: int = 0
    largest_other_cluster: int = 0  # size of the biggest group of alternatives that disagree with the candidate
    # The same three counts, in distinct sources. Several samples from one model are one voice.
    sources: int = 0
    agreeing_sources: int = 0
    largest_other_cluster_sources: int = 0
    # The same votes weighted by how reliable each source is known to be (see ``weights`` in
    # :func:`run`): the summed weight of the sources agreeing with the candidate, and of the
    # largest group of sources agreeing with one another against it.
    agreeing_weight: float = 0.0
    largest_other_cluster_weight: float = 0.0
    weighted: bool = False  # whether any source had a declared weight
    findings: list[Finding] = field(default_factory=list)

    @property
    def agreement(self) -> float | None:
        return self.agreeing / self.comparable if self.comparable else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparable": self.comparable,
            "agreeing": self.agreeing,
            "agreement": None if self.agreement is None else round(self.agreement, 4),
            "largest_disagreeing_cluster": self.largest_other_cluster,
            "sources": self.sources,
            "agreeing_sources": self.agreeing_sources,
            "largest_disagreeing_cluster_sources": self.largest_other_cluster_sources,
            "agreeing_weight": round(self.agreeing_weight, 3),
            "largest_disagreeing_cluster_weight": round(self.largest_other_cluster_weight, 3),
            "weighted": self.weighted,
            "alternatives": [a.to_dict() for a in self.alternatives],
        }


ZERO_TOLERANCE = 1e-9  # numbers closer to zero than this are zero
MAX_PERMUTATIONS = 5000  # column orders tried before giving up on matching two results


def _norm(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        number = float(value)
        if math.isnan(number):
            return "NaN"
        if abs(number) < ZERO_TOLERANCE:
            return 0  # 0.1 + 0.2 - 0.3 is 5.6e-17 in binary floating point
        if number.is_integer() and abs(number) < 1e15:
            return int(number)
        return float(f"{number:.9g}")
    if isinstance(value, _dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return value.isoformat(timespec="seconds")
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, str):
        return value
    return repr(value)


def _key(value: Any) -> tuple:
    if value is None:
        return (0, "")
    if isinstance(value, (int, float)):
        return (1, f"{float(value):+.9e}")
    return (2, str(value))


def _normed(rows: Sequence[Sequence[Any]]) -> list[tuple]:
    return [tuple(_norm(v) for v in row) for row in rows]


def _row_key(row: Sequence[Any]) -> tuple:
    return tuple(_key(v) for v in row)


def _column_signatures(rows: list[tuple], width: int) -> list[tuple]:
    return [tuple(sorted((_key(row[i]) for row in rows))) for i in range(width)]


def fingerprint(rows: Sequence[Sequence[Any]]) -> tuple[tuple, tuple]:
    """Two signatures of a result set that ignore row order.

    The first keeps column positions. The second also ignores column order: the
    columns are put in a fixed order worked out from their contents, so
    ``SELECT a, b`` and ``SELECT b, a`` match, while values that moved between
    columns in some rows only do not.
    """
    normed = _normed(rows)
    strict = tuple(sorted(normed, key=_row_key))
    width = len(normed[0]) if normed else 0
    signatures = _column_signatures(normed, width)
    order = sorted(range(width), key=lambda i: signatures[i])
    loose = tuple(sorted((tuple(row[i] for i in order) for row in normed), key=_row_key))
    return strict, loose


def _bound_by_name(left: Sequence[str] | None, right: Sequence[str] | None) -> list[int] | None:
    """When both results use the same column names, the names say which column is which."""
    if not left or not right or len(left) != len(right):
        return None
    a = [str(name).lower() for name in left]
    b = [str(name).lower() for name in right]
    if len(set(a)) != len(a) or len(set(b)) != len(b) or set(a) != set(b):
        return None
    return [b.index(name) for name in a]


def _permutations(base: list[tuple], other: list[tuple], width: int) -> Iterable[list[int]]:
    """Column orders of ``other`` that could line up with ``base``, judged column by column."""
    mine = _column_signatures(base, width)
    theirs = _column_signatures(other, width)
    options = [[j for j in range(width) if theirs[j] == mine[i]] for i in range(width)]
    if any(not choice for choice in options):
        return
    budget = [MAX_PERMUTATIONS]

    def walk(position: int, used: tuple[int, ...]) -> Iterable[list[int]]:
        if budget[0] <= 0:
            return
        if position == width:
            budget[0] -= 1
            yield list(used)
            return
        for j in options[position]:
            if j not in used:
                yield from walk(position + 1, used + (j,))

    yield from walk(0, ())


def compare(
    base: Sequence[Sequence[Any]],
    other: Sequence[Sequence[Any]],
    base_columns: Sequence[str] | None = None,
    other_columns: Sequence[str] | None = None,
) -> tuple[str, bool]:
    """Whether two results hold the same data: ``(AGREES | AGREES_REORDERED | DIFFERS, same row order)``.

    Row order never decides the answer; it is reported separately. Columns may come
    in another order, but one reordering has to work for every row. When both results
    carry the same column names, the names decide which column is which.
    """
    left, right = _normed(base), _normed(other)
    if len(left) != len(right):
        return DIFFERS, False
    width = len(left[0]) if left else len(base_columns or ())
    other_width = len(right[0]) if right else len(other_columns or ())
    if width != other_width:
        return DIFFERS, False
    left_sorted = sorted(left, key=_row_key)

    def under(order: list[int]) -> list[tuple]:
        return [tuple(row[j] for j in order) for row in right]

    named = _bound_by_name(base_columns, other_columns)
    if named is not None:
        moved = under(named)
        if sorted(moved, key=_row_key) != left_sorted:
            return DIFFERS, False
        return (AGREES if named == list(range(width)) else AGREES_REORDERED), moved == left
    if sorted(right, key=_row_key) == left_sorted:
        return AGREES, right == left
    for order in _permutations(left, right, width):
        moved = under(order)
        if sorted(moved, key=_row_key) == left_sorted:
            return AGREES_REORDERED, moved == left
    return DIFFERS, False


def _says_nothing(rows: Sequence[Sequence[Any]]) -> bool:
    """No rows at all, or nothing but NULL: two such results agreeing is not evidence."""
    return all(value is None for row in rows for value in row)


_QUOTED = re.compile(r"('(?:[^']|'')*')")


def _text_key(sql: str) -> str:
    """The statement with spacing and letter case evened out, except inside string literals."""
    parts = _QUOTED.split(sql.strip().rstrip(";").strip())
    return "".join(part if index % 2 else " ".join(part.lower().split()) for index, part in enumerate(parts))


def _row_limit(tree: exp.Expression | None) -> int | None:
    """N when the statement ends in a plain ``LIMIT N`` with no OFFSET."""
    if tree is None or tree.args.get("offset") is not None:
        return None
    limit = tree.args.get("limit")
    count = limit.expression if isinstance(limit, exp.Limit) else None
    if isinstance(count, exp.Literal) and not count.is_string and count.name.isdigit():
        return int(count.name)
    return None


def unstable(checked: A.Analysis, catalog: Catalog | None, rows_returned: int | None = None) -> str | None:
    """Why this statement's result may differ between two runs on the same data, or ``None``."""
    tree = checked.tree
    if tree is None:
        return None
    random = safety.nondeterministic(tree)
    if random:
        return f"it uses {', '.join(random)}"
    findings: list[Finding] = []
    if checked.resolved is not None and catalog is not None:
        try:
            from .checks import aggregation
            from .checks.base import CheckContext

            findings = list(aggregation.limit_without_order(CheckContext(checked, catalog, None)))
        except Exception:  # fall back to the plain reading below
            logger.debug("limit check failed on an alternative", exc_info=True)
            findings = []
    else:
        for node in tree.find_all(exp.Query):
            cut = node.args.get("limit") is not None or node.args.get("offset") is not None
            if cut and node.args.get("order") is None and not isinstance(node.parent, exp.Exists):
                return "it cuts rows off with no ORDER BY"
        return None
    if not findings:
        return None
    top = _row_limit(tree)
    only_at_the_end = all(not f.evidence.get("nested") for f in findings)
    if only_at_the_end and top is not None and rows_returned is not None and rows_returned < top:
        return None  # the LIMIT did not cut anything off
    return "it cuts rows off with no ORDER BY that says which"


def _has_order(tree: exp.Expression | None) -> bool:
    return tree is not None and tree.args.get("order") is not None


def describe_schema(catalog: Catalog, focus: Sequence[str] = (), limit: int = 150) -> str:
    """A compact text description of the schema, for a generator's prompt."""
    names = catalog.table_names()
    lines: list[str] = []
    if len(names) > limit:
        wanted = set(focus)
        names = [n for n in names if n in wanted] or names[:limit]
    for qualified in names:
        schema, _, name = qualified.rpartition(".")
        table = catalog.resolve(name, schema or None, quoted=True) or catalog.resolve(name, schema or None)
        if table is None:
            continue
        if table.is_view:
            columns = ", ".join(c.name for c in table.columns) or "..."
            lines.append(f"VIEW {qualified} ({columns})")
            continue
        parts = []
        for column in table.columns:
            flags = "" if column.nullable is not False else " NOT NULL"
            parts.append(f"{column.name} {column.type}{flags}".strip())
        if table.primary_key:
            parts.append(f"PRIMARY KEY ({', '.join(table.primary_key)})")
        for fk in table.foreign_keys:
            parts.append(f"FOREIGN KEY ({', '.join(fk.columns)}) REFERENCES {fk.ref_table} ({', '.join(fk.ref_columns)})")
        lines.append(f"TABLE {qualified} ({', '.join(parts)})")
    return "\n".join(lines)


def run(
    sql: str,
    alternatives: Sequence[str],
    *,
    dialect: str,
    catalog: Catalog | None,
    adapter: Adapter,
    max_rows: int = 5000,
    timeout_s: float = 15.0,
    cost_ceiling: float | None = None,
    max_alternatives: int = 8,
    role: str = "confirm",
    budget_s: float | None = 30.0,
    ordered: bool = False,
    allowed_functions: Iterable[str] = (),
    allowed_tables: Iterable[str] = (),
    weights: Mapping[str, float] | None = None,
) -> Outcome:
    """Run the candidate and each alternative, and compare what they return.

    ``role`` says how far the alternatives are trusted. ``confirm`` (the default):
    their agreement supports the candidate, and their disagreement is a doubt.
    ``judge``: when two or more independent sources agree with each other against
    the candidate, that is treated as strong evidence the candidate is wrong. Use
    ``judge`` only when the alternatives come from sources at least as capable as
    whatever wrote the candidate.

    ``budget_s`` caps the time spent on the whole comparison. ``ordered`` says the
    question asks for rows in a particular order, so the same rows in another order
    are a different answer.

    An alternative is run only if it is a single read-only query over known tables
    whose every function call is on the engine's allow-list.
    """
    outcome = Outcome()
    started = time.perf_counter()

    def remaining() -> float:
        if budget_s is None:
            return timeout_s
        return min(timeout_s, budget_s - (time.perf_counter() - started))

    seen: set[tuple[str, str | None]] = set()
    by_source: dict[str | None, list[tuple[str, str | None]]] = {}
    own = _text_key(sql)
    for item in alternatives:
        alt, source = split_alternative(item)
        if not alt:
            continue
        text = _text_key(alt)
        if text == own or (text, source) in seen:
            continue  # the candidate again, or a repeat from the same source: no new evidence
        seen.add((text, source))
        by_source.setdefault(source if source is not None else f"\0{len(seen)}", []).append((alt, source))
    # Take turns between sources, so a limit never leaves a whole source out.
    unique: list[tuple[str, str | None]] = []
    queues = list(by_source.values())
    while queues and len(unique) < max_alternatives:
        for queue in list(queues):
            if len(unique) >= max_alternatives:
                break
            unique.append(queue.pop(0))
            if not queue:
                queues.remove(queue)
    if not unique:
        outcome.reason = "no alternative differs from the candidate's own text"
        return outcome

    base = adapter.execute(sql, max_rows=max_rows, timeout_s=timeout_s)
    if not base.ok:
        outcome.reason = f"the candidate could not be run for comparison: {base.error}"
        return outcome
    if base.truncated:
        outcome.reason = f"the candidate returns more than {max_rows} rows, too many to compare"
        return outcome
    try:
        own_analysis = A.analyse(sql, catalog, dialect, allowed_functions, allowed_tables)
    except Exception:
        own_analysis = None
    if own_analysis is not None:
        why = unstable(own_analysis, catalog, len(base.rows))
        if why:
            outcome.reason = f"the query's result is not fixed from one run to the next ({why}), so comparing it says nothing"
            return outcome
    base_ordered = _has_order(own_analysis.tree) if own_analysis is not None else False
    base_empty = _says_nothing(base.rows)

    # Results that differ from the candidate's, grouped: (rows, columns) of the first of each group.
    groups: list[tuple[list[tuple], list[str]]] = []
    cluster_sizes: dict[int, int] = {}
    # For each source, how many of its comparable samples landed on each result. Result 0 is
    # the candidate's own.
    tallies: dict[str, dict[int, int]] = {}
    empty_matches = 0
    for position, (alt, source) in enumerate(unique):
        voice = source if source is not None else f"#{position}"  # unlabelled: each its own voice
        entry = Alternative(sql=alt, status=INVALID, source=source)
        outcome.alternatives.append(entry)
        try:
            checked = A.analyse(alt, catalog, dialect, allowed_functions, allowed_tables)
        except Exception as exc:  # an alternative must never break scoring
            entry.detail = f"could not be analysed: {type(exc).__name__}"
            continue
        if not checked.is_query or checked.unsafe_functions or checked.script_statements:
            entry.detail = "not a single read-only query"
            continue
        if checked.unlisted_functions:
            entry.detail = "calls a function not known to be read-only: " + ", ".join(checked.unlisted_functions)
            continue
        if catalog is not None and (checked.unknown_tables or checked.unknown_columns):
            entry.detail = "names a table or column that does not exist"
            continue
        if cost_ceiling is not None and checked.tables:
            total = 0
            known = True
            for table in checked.tables.values():
                try:
                    count = adapter.row_count(table)
                except SqlgamiError:
                    count = None
                if count is None:
                    known = False
                    break
                total += count
            if known and total > cost_ceiling:
                entry.status, entry.detail = SKIPPED, "estimated cost is above the ceiling"
                continue
        left = remaining()
        if left <= 0.05:
            entry.status, entry.detail = SKIPPED, "the time allowed for comparing alternatives ran out"
            continue
        try:
            result = adapter.execute(alt, max_rows=max_rows, timeout_s=left)
        except SqlgamiError as exc:
            entry.status, entry.detail = INVALID, str(exc)[:160]
            continue
        if not result.ok:
            entry.status, entry.detail = FAILED, (result.error or "")[:160]
            continue
        entry.rows = len(result.rows)
        if result.truncated:
            entry.status, entry.detail = SKIPPED, "result too large to compare"
            continue
        why = unstable(checked, catalog, len(result.rows))
        if why:
            entry.status, entry.detail = SKIPPED, f"its result is not fixed from one run to the next: {why}"
            continue
        verdict, same_order = compare(base.rows, result.rows, base.columns, result.columns)
        if verdict != DIFFERS and base_empty:
            # Two queries that both find nothing agree whatever they were looking for.
            entry.status, entry.detail = BOTH_EMPTY, "neither returns any value, which supports nothing"
            empty_matches += 1
            continue
        if verdict != DIFFERS and not same_order and base_ordered and _has_order(checked.tree):
            if ordered:
                verdict = DIFFERS
                entry.detail = "same rows in another order, and the question asks for an order"
            else:
                entry.status = AGREES_OTHER_ORDER
        outcome.comparable += 1
        tally = tallies.setdefault(voice, {})
        if verdict != DIFFERS:
            if entry.status != AGREES_OTHER_ORDER:
                entry.status = verdict
            outcome.agreeing += 1
            tally[0] = tally.get(0, 0) + 1
            continue
        entry.status = DIFFERS
        number = None
        if not _says_nothing(result.rows):  # results that hold nothing are never grouped together
            for index, (rows, columns) in enumerate(groups, 1):
                if compare(rows, result.rows, columns, result.columns)[0] != DIFFERS:
                    number = index
                    break
        if number is None:
            groups.append((list(result.rows), list(result.columns)))
            number = len(groups)
        entry.cluster = number
        cluster_sizes[number] = cluster_sizes.get(number, 0) + 1
        tally[number] = tally.get(number, 0) + 1
        entry.detail = entry.detail or _difference(base.rows, result.rows)

    outcome.ran = outcome.comparable > 0
    if not outcome.ran:
        outcome.reason = (
            "the query and its alternatives all return nothing, which is not evidence either way"
            if empty_matches
            else "no alternative could be run and compared"
        )
        return outcome
    outcome.largest_other_cluster = max(cluster_sizes.values(), default=0)
    # Each source casts one vote: the result most of its samples returned. A source whose
    # samples are split evenly between the candidate's result and another sides with the candidate.
    outcome.sources = len(tallies)
    votes_against: dict[int, int] = {}
    for tally in tallies.values():
        top = max(tally.values())
        if tally.get(0, 0) == top:
            outcome.agreeing_sources += 1
        else:
            winner = min(number for number, count in tally.items() if count == top)
            votes_against[winner] = votes_against.get(winner, 0) + 1
    outcome.largest_other_cluster_sources = max(votes_against.values(), default=0)
    # Weighted votes: a source's weight is its known share of right answers (0..1); a source with
    # no declared weight counts as DEFAULT_WEIGHT.
    weights = weights or {}
    outcome.weighted = any(source_weight(voice, weights) is not None for voice in tallies)
    against_weight: dict[int, float] = {}
    for voice, tally in tallies.items():
        w = source_weight(voice, weights)
        w = DEFAULT_WEIGHT if w is None else w
        top = max(tally.values())
        if tally.get(0, 0) == top:
            outcome.agreeing_weight += w
        else:
            winner = min(number for number, count in tally.items() if count == top)
            against_weight[winner] = against_weight.get(winner, 0.0) + w
    outcome.largest_other_cluster_weight = max(against_weight.values(), default=0.0)
    outcome.findings = _judge(outcome, role)
    return outcome


DEFAULT_WEIGHT = 0.5  # a source nothing is known about
WEAK_SOURCE = 0.5  # below this a source's lone disagreement is a note, not a doubt
STRONG_AGAINST = 1.2  # summed weight of sources agreeing with one another against the candidate that is a verdict


def source_weight(voice: str, weights: Mapping[str, float]) -> float | None:
    """The declared weight for a source name, matched exactly, then by substring either way
    (``"gemma"`` covers ``"hf:google/gemma-3-27b-it"``), clipped to 0..1."""
    if not weights:
        return None
    key = voice.lower()
    found = None
    if voice in weights:
        found = weights[voice]
    else:
        for name, value in weights.items():
            lowered = str(name).lower()
            if lowered and (lowered in key or key in lowered):
                found = value
                break
    if found is None:
        return None
    try:
        return min(1.0, max(0.0, float(found)))
    except (TypeError, ValueError):
        return None


def split_alternative(item: Any) -> tuple[str | None, str | None]:
    """An alternative may be SQL text, ``(sql, source)``, or ``{"sql": ..., "source": ...}``."""
    if isinstance(item, str):
        return (item if item.strip() else None), None
    if isinstance(item, dict):
        sql, source = item.get("sql"), item.get("source")
    elif isinstance(item, (tuple, list)) and len(item) == 2:
        sql, source = item
    else:
        return None, None
    if not isinstance(sql, str) or not sql.strip():
        return None, None
    return sql, (str(source) if source is not None else None)


def combine(*generators: Generator, names: Sequence[str] | None = None) -> Generator:
    """One generator out of several, tagging each alternative with which one wrote it.

    Use it to draw alternatives from different models, so that their agreement
    counts as agreement between independent voices.
    """

    def combined(question: str, schema: str, glossary: "str | None", k: int) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for index, generator in enumerate(generators):
            label = names[index] if names and index < len(names) else getattr(generator, "model", f"generator {index + 1}")
            try:
                produced = generator(question, schema, glossary, k) or []
            except Exception:
                logger.debug("one of the combined generators failed", exc_info=True)
                continue
            for item in produced:
                sql, source = split_alternative(item)
                if sql:
                    found.append((sql, source or str(label)))
        return found

    return combined


def _difference(base: Sequence[Sequence[Any]], other: Sequence[Sequence[Any]]) -> str:
    if len(base) != len(other):
        return f"returns {len(other)} rows where the candidate returns {len(base)}"
    if base and other and len(base[0]) != len(other[0]):
        return f"returns {len(other[0])} columns where the candidate returns {len(base[0])}"
    return "same shape, different values"


def _judge(outcome: Outcome, role: str = "confirm") -> list[Finding]:
    """Turn the counts into findings. Votes are counted in distinct sources, and weighed by what
    is known of each source: two reliable sources agreeing with each other against the candidate
    is a verdict whatever the role; one unreliable source disagreeing is a note."""
    n, agree, rival = outcome.sources, outcome.agreeing_sources, outcome.largest_other_cluster_sources
    evidence = {
        "comparable": outcome.comparable,
        "agreeing": outcome.agreeing,
        "sources": n,
        "agreeing_sources": agree,
        "largest_disagreeing_cluster_sources": rival,
        "agreeing_weight": round(outcome.agreeing_weight, 3),
        "largest_disagreeing_cluster_weight": round(outcome.largest_other_cluster_weight, 3),
    }
    if agree and agree >= rival and agree * 2 >= n:
        return []  # the candidate is in the largest group: support, expressed through the score
    if outcome.weighted and rival >= 2 and rival > agree and outcome.largest_other_cluster_weight >= STRONG_AGAINST:
        return [
            Finding(
                "consistency_disagreement",
                Severity.HIGH,
                f"{rival} of {n} independent sources, each known to be right most of the time, wrote "
                "queries for this question that return the same result as one another, and it is not "
                "what this query returns" + (f" ({agree} agree with this query)." if agree else "."),
                "consistency",
                suggestion="Compare this query with the alternatives listed under signals.consistency.",
                evidence={**evidence, "role": role, "verdict": "reliable sources agree against the candidate"},
            )
        ]
    if outcome.weighted and n == 1 and outcome.largest_other_cluster_weight < WEAK_SOURCE and not agree:
        return [
            Finding(
                "consistency_disagreement",
                Severity.LOW,
                "The one independently written query for this question returns a different result, but "
                "its source is right less than half the time, so this is a note rather than a doubt.",
                "consistency",
                evidence=evidence,
            )
        ]
    if rival >= 2 and rival > agree:
        judged = role == "judge"
        return [
            Finding(
                "consistency_disagreement",
                Severity.HIGH if judged else Severity.MEDIUM,
                f"{rival} of {n} independent sources wrote queries for this question that return the same "
                "result as one another, and it is not what this query returns"
                + (f" ({agree} agree with this query)." if agree else ".")
                + (
                    ""
                    if judged
                    else " Whether that makes this query wrong depends on how capable those sources are."
                ),
                "consistency",
                suggestion="Compare this query with the alternatives listed under signals.consistency.",
                evidence={**evidence, "role": role},
            )
        ]
    if n == 1:
        several = outcome.largest_other_cluster >= 2
        return [
            Finding(
                "consistency_disagreement",
                Severity.MEDIUM,
                (
                    f"{outcome.largest_other_cluster} queries from one other source agree with each other "
                    "and return a different result. Samples from a single source share their mistakes, so "
                    "this is a doubt, not a verdict."
                    if several
                    else "The one independently written query for this question returns a different result. "
                    "One of the two is wrong, or the question can be read two ways."
                ),
                "consistency",
                evidence=evidence,
            )
        ]
    return [
        Finding(
            "consistency_no_consensus",
            Severity.MEDIUM,
            f"Queries from {n} independent sources were compared and there is no clear majority among their "
            f"results ({agree} match this query). The question may be ambiguous.",
            "consistency",
            suggestion="Tighten the question, or review the alternatives listed under signals.consistency.",
            evidence=evidence,
        )
    ]
