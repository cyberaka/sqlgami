"""Judge a candidate against the host's term catalogue (see :mod:`sqlgami.terms`).

For every term or KPI whose phrase the question uses, the candidate's SQL is compared with the
term's declared representation — by structure, never by text:

* ``filter``: a condition on the same column that means the same, NULL behaviour included.
  ``status_cd != 'L'`` against ``status_cd IS DISTINCT FROM 'L'`` is a contradiction (NULL rows
  are dropped), not a near miss.
* ``window``: the range the query puts on the column has the declared size or offset.
* ``source``: the column is read from the table the catalogue names.
* ``formula`` / KPI ``format``: the projection named for the term has the declared shape
  (an integer, floored not rounded, subtracting in the declared direction).
* a term with no ``sql`` (documentation only): nothing can be checked, and the report says so.

Terms of kind ``chain`` are folded into required-predicate rules by the Scorer and judged there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from .checks.base import CheckContext
from .model import SelectModel, flatten_and, flatten_or, strip, unalias, walk_own
from .report import Finding, Severity
from .terms import KPI, Catalogue, Match, Term, bound_number, parse_term_sql

SATISFIED, CONTRADICTED, MISSING, DIFFERS, NOT_READ, UNVERIFIED = (
    "satisfied",
    "contradicted",
    "missing",
    "differs",
    "source_not_read",
    "unverified",
)


@dataclass
class TermOutcome:
    term: str
    kind: str
    status: str
    detail: str = ""
    phrase: str = ""
    bindings: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"term": self.term, "kind": self.kind, "status": self.status, "detail": self.detail, "phrase": self.phrase, "bindings": self.bindings}


def check_terms(
    ctx: CheckContext, question: str, catalogue: Catalogue, dialect: str, params: dict[str, Any] | None = None
) -> tuple[list[Finding], dict[str, Any]]:
    """Findings and the ``signals["terms"]`` entry for one candidate."""
    matches = catalogue.match(question)
    outcomes: list[TermOutcome] = []
    findings: list[Finding] = []
    seen: set[str] = set()
    for match in matches:
        if match.name.lower() in seen:
            continue
        seen.add(match.name.lower())
        term = catalogue.term(match.name)
        kpi = catalogue.kpi(match.name) if term is None else None
        if term is not None:
            outcome, finding = _judge_term(ctx, term, match, dialect, params, catalogue)
        elif kpi is not None:
            outcome, finding = _judge_kpi(ctx, kpi, match, catalogue, dialect)
        else:
            continue
        outcomes.append(outcome)
        if finding is not None:
            findings.append(finding)
        if kpi is not None:
            # The KPI's required terms are judged as if the question had asked for each of them.
            for name in kpi.requires:
                required = catalogue.term(name)
                if required is None or required.name.lower() in seen:
                    continue
                seen.add(required.name.lower())
                implied = Match(required.name, f"{match.phrase} (requires {required.name})", match.span, {}, required.kind if required.enforceable else "")
                outcome, finding = _judge_term(ctx, required, implied, dialect, params, catalogue)
                outcomes.append(outcome)
                if finding is not None:
                    findings.append(finding)
    signal = {
        "catalogue": catalogue.to_dict(),
        "matched": [m.to_dict() for m in matches],
        "outcomes": [o.to_dict() for o in outcomes],
    }
    return findings, signal


# ------------------------------------------------------------------- terms


def _judge_term(
    ctx: CheckContext, term: Term, match: Match, dialect: str, params: dict[str, Any] | None, catalogue: Catalogue | None = None
) -> tuple[TermOutcome, Finding | None]:
    outcome = TermOutcome(term.name, term.kind, UNVERIFIED, phrase=match.phrase, bindings=match.bindings)
    if not term.enforceable:
        outcome.detail = "the catalogue describes this term but gives no data-level meaning"
        return outcome, Finding(
            "undefined_term",
            Severity.LOW,
            f'The question uses "{match.phrase}" ({term.name}), which the catalogue describes but does not '
            "define in data terms, so sqlgami cannot confirm the query reads it correctly.",
            "question",
            suggestion=f"Give the term {term.name} its sql in the catalogue (a filter, formula, window or source).",
            evidence={"term": term.name, "phrase": match.phrase},
        )
    if term.kind == "chain":
        return _judge_chain(ctx, term, match, dialect, outcome)
    if term.table and term.table.strip() != "*" and not _reads(ctx, term.table) and not _reads_column_of(ctx, term, dialect):
        outcome.status = NOT_READ
        outcome.detail = f"the query reads none of {term.table}"
        return outcome, Finding(
            "term_source_not_read",
            Severity.MEDIUM,
            f'The question asks about "{match.phrase}" ({term.name}), which the catalogue defines on '
            f"{term.table}, and the query does not read that table.",
            "question",
            evidence={"term": term.name, "table": term.table},
        )
    try:
        node = parse_term_sql(_bind(term, match.bindings), dialect)
    except Exception as exc:  # the catalogue was validated; a dialect override may still be odd
        outcome.detail = f"the term's sql could not be read: {exc}"
        return outcome, None
    if node is None:
        return outcome, None
    if term.kind == "filter":
        return _judge_filter(ctx, term, match, node, outcome, negated=_negated(ctx.question or "", match), catalogue=catalogue, dialect=dialect)
    if term.kind == "window":
        return _judge_window(ctx, term, match, node, outcome)
    if term.kind == "source":
        return _judge_source(ctx, term, match, node, outcome)
    if term.kind == "formula":
        return _judge_formula(ctx, term, match, node, outcome)
    if term.kind == "rank":
        return _judge_rank(ctx, term, match, node, outcome, params)
    return outcome, None  # chain: judged by the required-predicate rule it was folded into


def _judge_chain(ctx: CheckContext, term: Term, match: Match, dialect: str, outcome: TermOutcome) -> tuple[TermOutcome, Finding | None]:
    """An access chain, asked for by the question: an OR chain over the listed columns (any
    table that has them, or the declared tables) against one value — the required-predicate
    rule, applied because the term was matched. A one-column chain is a plain filter."""
    from .catalog import RequiredPredicate
    from .checks.policy import _or_chain_rule

    left, _, right = term.sql_for(dialect).rpartition("=")
    columns = tuple(c.strip() for c in left.split(",") if c.strip())
    if not columns:
        return outcome, None
    if len(columns) == 1:
        single = Term(term.name, term.phrases, "filter", term.table, f"{columns[0]} = {right.strip()}", term.nulls, term.description, term.dialects, term.row, term.qualifiers)
        try:
            node = parse_term_sql(single, dialect)
        except Exception:
            return outcome, None
        if node is None:
            return outcome, None
        return _judge_filter(ctx, single, match, node, outcome, negated=False, catalogue=None, dialect=dialect)
    tables = tuple(t.strip() for t in term.table.split(";") if t.strip() and t.strip() != "*")
    rule = RequiredPredicate(term.name, columns, tables, "or_chain", "high", term.description)
    if not any(rule.applies_to(t) for t in ctx.analysis.tables.values()):
        outcome.status = NOT_READ
        outcome.detail = "the query reads no table with those columns"
        return outcome, Finding(
            "term_source_not_read",
            Severity.MEDIUM,
            f'The question asks about "{match.phrase}" ({term.name}), an access chain over {", ".join(columns[:4])}…, and the query reads no table that has those columns.',
            "question",
            evidence={"term": term.name},
        )
    findings = list(_or_chain_rule(ctx, rule, Severity.HIGH, term.name))
    if findings:
        outcome.status = CONTRADICTED if "leaves out" in findings[0].message else MISSING
        outcome.detail = findings[0].message[:160]
        finding = findings[0]
        finding.stage = "question"
        finding.evidence["term"] = term.name
        return outcome, finding
    outcome.status = SATISFIED
    return outcome, None


def _bind(term: Term, bindings: dict[str, str]) -> Term:
    """The term with the question's numbers in place of ``{n}`` / ``{m}``."""
    sql = term.sql
    dialects = dict(term.dialects)
    for key, value in bindings.items():
        number = bound_number(value)
        if number is None:
            continue
        sql = sql.replace("{" + key + "}", str(number))
        dialects = {d: s.replace("{" + key + "}", str(number)) for d, s in dialects.items()}
    return Term(term.name, term.phrases, term.kind, term.table, sql, term.nulls, term.description, dialects, term.row, term.qualifiers)


def _reads_column_of(ctx: CheckContext, term: Term, dialect: str) -> bool:
    """The term is declared on one table but its column exists on a table the query does read
    (tenant_id, deleted_at and the like live on many tables): judge it there."""
    if term.kind not in ("filter", "window"):
        return False
    try:
        node = parse_term_sql(term, dialect)
    except Exception:
        return False
    if node is None:
        return False
    columns = {c.name.lower() for c in node.find_all(exp.Column)}
    if not columns:
        return False
    for table in ctx.analysis.tables.values():
        names = {ctx.catalog.column_key(c.name) for c in table.columns}
        if columns <= names:
            return True
    return False


def _reads(ctx: CheckContext, tables: str) -> bool:
    wanted = {t.strip().lower() for t in tables.split(";") if t.strip()}
    read: set[str] = set()
    for table in ctx.analysis.tables.values():
        read.add(table.qualified_name.lower())
        read.add(table.name.lower())
    return any(w in read or w.split(".")[-1] in read for w in wanted)


# ------------------------------------------------------------------ filters


@dataclass
class _Cond:
    """A condition on one column in canonical form."""

    column: str
    op: str  # eq | ne | in | not_in | is_null | not_null | other
    values: tuple[Any, ...]
    nulls_kept: bool | None  # for ne / not_in: whether NULL rows pass
    text: str


def _canonical(node: exp.Expression) -> _Cond | None:
    node = strip(node)
    if isinstance(node, exp.Paren):
        node = strip(node.this)
    # (col <> v OR col IS NULL)
    if isinstance(node, exp.Or):
        branches = [strip(b) for b in flatten_or(node)]
        nulls = [b for b in branches if isinstance(b, exp.Is) and isinstance(b.expression, exp.Null)]
        others = [b for b in branches if b not in nulls]
        if len(nulls) == 1 and len(others) == 1:
            inner = _canonical(others[0])
            null_col = strip(nulls[0].this)
            if inner and isinstance(null_col, exp.Column) and null_col.name.lower() == inner.column and inner.op in ("ne", "not_in"):
                return _Cond(inner.column, inner.op, inner.values, True, node.sql())
        return None
    if isinstance(node, exp.Not):
        inner = _canonical(node.this)
        if inner is None:
            return None
        flipped = {"eq": "ne", "ne": "eq", "in": "not_in", "not_in": "in", "is_null": "not_null", "not_null": "is_null"}
        op = flipped.get(inner.op, "other")
        kept = False if op in ("ne", "not_in") else None
        return _Cond(inner.column, op, inner.values, kept, node.sql())
    if isinstance(node, exp.Is):
        column = _column_of(node.this)
        if column is None:
            return None
        return _Cond(column, "is_null" if isinstance(node.expression, exp.Null) else "other", (), None, node.sql())
    if isinstance(node, (exp.EQ, exp.NEQ, exp.NullSafeEQ, exp.NullSafeNEQ)):
        left, right = strip(node.this), strip(node.expression)
        column, value = _column_of(left), right
        if column is None:
            column, value = _column_of(right), left
        if column is None or not _is_literal(value):
            return None
        null_safe = isinstance(node, (exp.NullSafeEQ, exp.NullSafeNEQ))
        negative = isinstance(node, (exp.NEQ, exp.NullSafeNEQ))
        coalesced = isinstance(left, exp.Coalesce) or isinstance(right, exp.Coalesce)
        op = "ne" if negative else "eq"
        kept = (null_safe or coalesced) if negative else None
        return _Cond(column, op, (_value(value),), kept, node.sql())
    if isinstance(node, exp.In) and not node.args.get("query") and node.expressions:
        column = _column_of(node.this)
        if column is None or not all(_is_literal(e) for e in node.expressions):
            return None
        coalesced = isinstance(strip(node.this), exp.Coalesce)
        values = tuple(sorted((_value(e) for e in node.expressions), key=repr))
        op = "eq" if len(values) == 1 else "in"
        return _Cond(column, op, values, True if coalesced else None, node.sql())
    return None


def _column_of(node: exp.Expression) -> str | None:
    node = strip(node)
    if isinstance(node, exp.Coalesce):
        node = strip(node.this)
    if isinstance(node, exp.Cast):
        node = strip(node.this)
    if isinstance(node, exp.Column) and not _is_placeholder_column(node):
        return node.name.lower()
    return None


def _is_placeholder_column(node: exp.Expression) -> bool:
    """A term's ``{param}`` after parsing (``sqlgami_ph_param``), or the statement's own marker."""
    return isinstance(node, exp.Column) and node.name.lower().startswith("sqlgami_ph_")


def _is_literal(node: exp.Expression) -> bool:
    node = strip(node)
    return isinstance(node, (exp.Literal, exp.Null, exp.Boolean, exp.Placeholder, exp.Parameter)) or _is_placeholder_column(node) or (
        isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal)
    )


def _value(node: exp.Expression) -> Any:
    node = strip(node)
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.name
        try:
            return float(node.name)
        except ValueError:
            return node.name
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        try:
            return -float(node.this.name)
        except ValueError:
            return node.sql()
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Null):
        return None
    if _is_placeholder_column(node):
        return "{" + node.name[len("sqlgami_ph_"):] + "}"
    if isinstance(node, (exp.Placeholder, exp.Parameter)):
        name = node.name if isinstance(node.this, str) else getattr(node.this, "name", "")
        return "{" + str(name) + "}"
    return node.sql()


def _conditions(ctx: CheckContext, column: str) -> list[tuple[_Cond, SelectModel]]:
    """Every canonical condition on ``column`` that gates rows anywhere in the statement: WHERE and
    ON conjuncts of every SELECT (CTEs and subqueries included), HAVING, the FILTER (WHERE ...)
    of an aggregate, and the WHEN of a CASE inside an aggregate or a projection — the forms
    production SQL uses for conditional measures (``SUM(CASE WHEN invol_exit_ind = 1 THEN 1
    ELSE 0 END)``, ``COUNT(DISTINCT CASE WHEN status_cd = 'A' THEN assoc_id END)``)."""
    found: list[tuple[_Cond, SelectModel]] = []
    for model in ctx.cache.models():
        tests: list[exp.Expression] = []
        for conjunct in model.conjuncts:
            tests.extend(flatten_and(conjunct.expr))
        having = model.select.args.get("having")
        if having is not None:
            tests.extend(flatten_and(having.this))
        for node in model.own():
            if isinstance(node, exp.Filter):
                where = node.args.get("expression")
                if where is not None:
                    tests.extend(flatten_and(where.this if isinstance(where, exp.Where) else where))
            elif isinstance(node, exp.If) and isinstance(node.parent, exp.Case):
                tests.extend(flatten_and(node.this))
            elif isinstance(node, exp.Anonymous) and (node.name or "").lower() in ("countif", "count_if") and node.expressions:
                tests.extend(flatten_and(node.expressions[0]))
        for part in tests:
            cond = _canonical(part)
            if cond is not None and cond.column == column:
                found.append((cond, model))
        # A column equated through a join to one pinned to a value (invoices.tenant_id = t.id
        # AND t.id = {tenant_id}) is filtered to that value too.
        for ref, value in model.bound.items():
            if not _is_literal(value):
                continue
            for member in [ref, *model.class_members(ref)]:
                if member != ref and member[1].lower() == column:
                    text = f"{member[0]}.{member[1]} = {ref[0]}.{ref[1]} = {value.sql()}"
                    found.append((_Cond(column, "eq", (_value(value),), None, text), model))
                    break
    return found


_NEGATIONS_BEFORE = re.compile(r"\b(excluding|exclude|excludes|except|without|not|non|omit|omitting)\s*$")
_NEGATIONS_AFTER = re.compile(r"^\s*\w*\s*(?:are |is |rows |ones )?(excluded|omitted|not included|left out)\b")


def _negated(question: str, match: Match) -> bool:
    """Whether the question negates the matched phrase — "excluding soft-deleted rows",
    "non-contractors", "soft-deleted subscriptions excluded", "not on leave" when the term
    itself is "on leave" — so the expected condition is the inverse. Only an immediate negation
    counts; "no current address" as the label of a bucket does not."""
    from .terms import normalise

    text = normalise(question)
    before = text[: match.span[0]].rstrip()
    after = text[match.span[1]:]
    return bool(_NEGATIONS_BEFORE.search(before[-24:])) or bool(_NEGATIONS_AFTER.match(after[:40]))


def _inverse(cond: _Cond) -> _Cond:
    flipped = {"eq": "ne", "ne": "eq", "in": "not_in", "not_in": "in", "is_null": "not_null", "not_null": "is_null"}
    op = flipped.get(cond.op, "other")
    return _Cond(cond.column, op, cond.values, None, f"NOT ({cond.text})")


def _declared_elsewhere(catalogue: Catalogue | None, cond: _Cond, dialect: str, exclude: str) -> str | None:
    """The name of another catalogue term whose condition is exactly ``cond`` — the query may be
    applying that term (the default "not deleted" rather than "deleted")."""
    if catalogue is None:
        return None
    for other in catalogue.terms:
        if other.name.lower() == exclude.lower() or other.kind != "filter" or not other.enforceable:
            continue
        try:
            node = parse_term_sql(other, dialect)
        except Exception:
            continue
        declared = _canonical(node) if node is not None else None
        if declared is None:
            continue
        if other.nulls == "include" and declared.op in ("ne", "not_in"):
            declared.nulls_kept = True
        elif other.nulls == "exclude" and declared.op in ("ne", "not_in"):
            declared.nulls_kept = False
        if _same_condition(cond, declared):
            return other.name
    return None


def _judge_filter(
    ctx: CheckContext,
    term: Term,
    match: Match,
    node: exp.Expression,
    outcome: TermOutcome,
    negated: bool = False,
    catalogue: Catalogue | None = None,
    dialect: str = "",
) -> tuple[TermOutcome, Finding | None]:
    wanted = _canonical(node)
    if wanted is None:
        outcome.detail = "the term's condition is not a simple test on one column; not checked"
        return outcome, None
    if term.nulls == "include" and wanted.op in ("ne", "not_in"):
        wanted.nulls_kept = True
    elif term.nulls == "exclude" and wanted.op in ("ne", "not_in"):
        wanted.nulls_kept = False
    expected_text = term.sql
    if negated:
        # "excluding X": the query must apply the opposite of X. What happens to NULL rows under
        # the negation is not declared, so it is not judged.
        wanted = _inverse(wanted)
        expected_text = f"NOT ({term.sql})"
        outcome.detail = "the question negates the term"
    found = _conditions(ctx, wanted.column)
    if not found:
        inside_or = _tested_inside_or(ctx, wanted.column)
        outcome.status = MISSING
        outcome.detail = (
            f"{wanted.column} is tested only inside an OR branch"
            if inside_or
            else f"nothing in the query filters on {wanted.column}"
        )
        return outcome, Finding(
            "term_missing",
            Severity.HIGH,
            f'The question asks for "{match.phrase}" ({term.name}), which the catalogue defines as {expected_text}; '
            + (
                f"the only test on {wanted.column} sits inside an OR branch ({inside_or}), so it does not apply to every row."
                if inside_or
                else f"the query applies no such condition on {wanted.column}."
            ),
            "question",
            snippet=inside_or or None,
            suggestion=f"Apply {expected_text} to the rows that reach the answer.",
            evidence={"term": term.name, "expected": expected_text},
        )
    for cond, _ in found:
        if _same_condition(cond, wanted):
            outcome.status = SATISFIED
            outcome.detail = cond.text
            return outcome, None
    contradiction = next((c for c, _ in found if _contradicts(c, wanted)), None)
    if contradiction is not None and not negated:
        sibling = _declared_elsewhere(catalogue, contradiction, dialect, term.name)
        if sibling is not None:
            # The question mentions the term, and the query applies the catalogue's OTHER term on
            # that column ("deleted" mentioned in passing, rows filtered to not_deleted): the
            # author declared both, so this is a note, not a contradiction.
            outcome.status = DIFFERS
            outcome.detail = f"applies {sibling} instead"
            return outcome, Finding(
                "term_differs",
                Severity.LOW,
                f'The question mentions "{match.phrase}" ({term.name}, {term.sql}); the query applies the '
                f"catalogue's {sibling} ({contradiction.text}) on that column instead.",
                "question",
                snippet=contradiction.text,
                evidence={"term": term.name, "applied": sibling, "found": contradiction.text},
            )
    if contradiction is not None:
        outcome.status = CONTRADICTED
        outcome.detail = contradiction.text
        why = _why(contradiction, wanted)
        return outcome, Finding(
            "term_contradicted",
            Severity.HIGH,
            f'The catalogue defines "{match.phrase}" ({term.name}) as {expected_text}; the query has '
            f"{contradiction.text}, {why}",
            "question",
            snippet=contradiction.text,
            suggestion=f"Write the condition as {expected_text}.",
            evidence={"term": term.name, "expected": expected_text, "found": contradiction.text},
        )
    outcome.status = DIFFERS
    outcome.detail = found[0][0].text
    return outcome, Finding(
        "term_differs",
        Severity.LOW,
        f'The catalogue defines "{match.phrase}" ({term.name}) as {expected_text}; the query filters '
        f"{wanted.column} differently ({found[0][0].text}), which may or may not mean the same.",
        "question",
        snippet=found[0][0].text,
        evidence={"term": term.name, "expected": expected_text, "found": found[0][0].text},
    )


def _tested_inside_or(ctx: CheckContext, column: str) -> str | None:
    """The text of a test on ``column`` that sits inside an OR, if that is the only place it is tested."""
    for model in ctx.cache.models():
        for conjunct in model.conjuncts:
            for part in flatten_and(conjunct.expr):
                if isinstance(strip(part), exp.Or):
                    for branch in flatten_or(strip(part)):
                        for piece in flatten_and(branch):
                            cond = _canonical(piece)
                            if cond is not None and cond.column == column:
                                return cond.text
    return None


def _same_condition(a: _Cond, b: _Cond) -> bool:
    if a.column != b.column or a.op != b.op or tuple(a.values) != tuple(b.values):
        return False
    if a.op in ("ne", "not_in") and b.nulls_kept is not None:
        return (a.nulls_kept or False) == b.nulls_kept
    return True


def _contradicts(found: _Cond, wanted: _Cond) -> bool:
    if found.column != wanted.column:
        return False
    if found.op == wanted.op and tuple(found.values) == tuple(wanted.values):
        return wanted.nulls_kept is not None and (found.nulls_kept or False) != wanted.nulls_kept
    inverse = {"eq": "ne", "ne": "eq", "in": "not_in", "not_in": "in", "is_null": "not_null", "not_null": "is_null"}
    return inverse.get(wanted.op) == found.op and tuple(found.values) == tuple(wanted.values)


def _why(found: _Cond, wanted: _Cond) -> str:
    if found.op == wanted.op:
        if wanted.nulls_kept:
            return "which drops rows where the column is NULL; the catalogue counts them."
        return "which keeps rows where the column is NULL; the catalogue excludes them."
    return "which selects the opposite rows."


# ------------------------------------------------------------------ windows


@dataclass
class _Range:
    column: str
    lower: float | None
    upper: float | None
    point: float | None = None
    preceding: int | None = None
    text: str = ""

    @property
    def size(self) -> float | None:
        if self.lower is not None and self.upper is not None:
            return self.upper - self.lower + 1
        return None


def _number(node: exp.Expression) -> float | None:
    """A numeric constant, with the arithmetic a catalogue writes (``-(26-1)``) folded."""
    node = strip(node)
    try:
        if isinstance(node, exp.Literal) and not node.is_string:
            return float(node.name)
        if isinstance(node, exp.Neg):
            inner = _number(node.this)
            return None if inner is None else -inner
        if isinstance(node, exp.Paren):
            return _number(node.this)
        if isinstance(node, (exp.Add, exp.Sub, exp.Mul)):
            left, right = _number(node.this), _number(node.expression)
            if left is None or right is None:
                return None
            return left + right if isinstance(node, exp.Add) else left - right if isinstance(node, exp.Sub) else left * right
    except (TypeError, ValueError):
        return None
    return None


def _constant_of(model: SelectModel | None, node: exp.Expression) -> float | None:
    """A number, or a column that a derived source (``params AS (SELECT -25 AS from_seq ...)``)
    defines as one."""
    value = _number(node)
    if value is not None or model is None:
        return value
    node = strip(node)
    if isinstance(node, exp.Column) and node.table in model.sources:
        source = model.sources[node.table]
        if source.kind == "derived" and isinstance(source.node, exp.Select):
            for projection in source.node.expressions:
                if projection.alias_or_name == node.name:
                    return _number(unalias(projection))
    return None


def _range_of(node: exp.Expression, model: SelectModel | None = None) -> _Range | None:
    node = strip(node)
    if isinstance(node, exp.Between):
        column = _column_of(node.this)
        low, high = _constant_of(model, node.args["low"]), _constant_of(model, node.args["high"])
        if column and low is not None and high is not None:
            return _Range(column, low, high, text=node.sql())
    if isinstance(node, exp.EQ):
        for side, other in ((node.this, node.expression), (node.expression, node.this)):
            column = _column_of(side)
            value = _constant_of(model, other)
            if column and value is not None:
                return _Range(column, None, None, point=value, text=node.sql())
    if isinstance(node, (exp.GTE, exp.GT, exp.LTE, exp.LT)):
        column = _column_of(node.this)
        value = _constant_of(model, node.expression)
        if column and value is not None:
            if isinstance(node, exp.GTE):
                return _Range(column, value, None, text=node.sql())
            if isinstance(node, exp.GT):
                return _Range(column, value + 1, None, text=node.sql())
            if isinstance(node, exp.LTE):
                return _Range(column, None, value, text=node.sql())
            return _Range(column, None, value - 1, text=node.sql())
    return None


def _frame_preceding(spec: exp.WindowSpec) -> int | None:
    start = spec.args.get("start")
    side = str(spec.args.get("start_side") or "").upper()
    if start is None or side != "PRECEDING":
        return None
    value = _number(start) if isinstance(start, exp.Expression) else None
    return int(value) if value is not None else None


def _ranges(ctx: CheckContext, column: str) -> list[_Range]:
    """Ranges the query puts on ``column``: BETWEEN, pairs of inequalities in one SELECT, points."""
    found: list[_Range] = []
    for model in ctx.cache.models():
        partial: list[_Range] = []
        for conjunct in model.conjuncts:
            for part in flatten_and(conjunct.expr):
                r = _range_of(part, model)
                if r is None or r.column != column:
                    continue
                if r.point is not None or (r.lower is not None and r.upper is not None):
                    found.append(r)
                else:
                    partial.append(r)
        lows = [p for p in partial if p.lower is not None]
        highs = [p for p in partial if p.upper is not None]
        for low in lows:
            for high in highs:
                found.append(_Range(column, low.lower, high.upper, text=f"{low.text} AND {high.text}"))
        if lows and not highs:
            found.extend(lows)
        if highs and not lows:
            found.extend(highs)
    return found


def _judge_window(ctx: CheckContext, term: Term, match: Match, node: exp.Expression, outcome: TermOutcome) -> tuple[TermOutcome, Finding | None]:
    expected_text = _bind(term, match.bindings).sql
    if isinstance(node, exp.WindowSpec):
        preceding = _frame_preceding(node)
        frames = [w.args.get("spec") for w in ctx.analysis.resolved.find_all(exp.Window) if w.args.get("spec") is not None]
        found = [(_frame_preceding(s), s.sql()) for s in frames]
        found = [(p, t) for p, t in found if p is not None]
        if preceding is None:
            outcome.detail = "the term's frame is not a plain N PRECEDING; not checked"
            return outcome, None
        if not found:
            # A rolling measure can be written without a frame (a self-join, a correlated
            # subquery); the absence of a frame is a note, a wrong frame is the finding.
            outcome.status = MISSING
            outcome.detail = "no window frame in the query"
            return outcome, Finding(
                "term_missing",
                Severity.LOW,
                f'The question asks for "{match.phrase}" ({term.name}), defined as {expected_text}; the query has no '
                "window frame, so the rolling measure, if computed another way, could not be checked against it.",
                "question",
                evidence={"term": term.name, "expected": expected_text},
            )
        if any(p == preceding for p, _ in found):
            outcome.status = SATISFIED
            return outcome, None
        p, t = found[0]
        outcome.status = CONTRADICTED
        outcome.detail = t
        return outcome, Finding(
            "term_window",
            Severity.HIGH,
            f'The catalogue defines "{match.phrase}" ({term.name}) as {expected_text} ({preceding + 1} rows); '
            f"the query's frame is {t} ({p + 1} rows).",
            "question",
            snippet=t,
            evidence={"term": term.name, "expected": expected_text, "found": t},
        )
    wanted = _range_of(node)
    if wanted is None:
        outcome.detail = "the term's range is not a plain range on one column; not checked"
        return outcome, None
    found = _ranges(ctx, wanted.column)
    if not found:
        outcome.status = MISSING
        outcome.detail = f"no range on {wanted.column}"
        return outcome, Finding(
            "term_missing",
            Severity.MEDIUM,
            f'The question asks for "{match.phrase}" ({term.name}), defined as {expected_text}, and the query puts no range on {wanted.column}.',
            "question",
            evidence={"term": term.name, "expected": expected_text},
        )
    for r in found:
        if _same_range(r, wanted):
            outcome.status = SATISFIED
            outcome.detail = r.text
            return outcome, None
    same_shape = [r for r in found if (r.point is not None) == (wanted.point is not None)]
    if not same_shape:
        outcome.status = DIFFERS
        outcome.detail = found[0].text
        return outcome, None  # a range of another shape may belong to another term
    r = same_shape[0]
    outcome.status = CONTRADICTED
    outcome.detail = r.text
    if wanted.point is not None:
        description = f"the query uses {r.text}"
    else:
        description = f"the query uses {r.text}" + (f" ({int(r.size)} rows of the sequence)" if r.size is not None else "")
    size_note = f" ({int(wanted.size)} rows of the sequence)" if wanted.size is not None else ""
    return outcome, Finding(
        "term_window",
        Severity.HIGH,
        f'The catalogue defines "{match.phrase}" ({term.name}) as {expected_text}{size_note}; {description}.',
        "question",
        snippet=r.text,
        suggestion=f"Write the range as {expected_text}.",
        evidence={"term": term.name, "expected": expected_text, "found": r.text},
    )


def _same_range(a: _Range, b: _Range) -> bool:
    if a.column != b.column:
        return False
    if b.point is not None:
        return a.point == b.point
    return a.lower == b.lower and a.upper == b.upper


# --------------------------------------------------------------------- rank

_RANK_FUNCTIONS = {"RANK": "rank", "DENSE_RANK": "dense_rank", "ROW_NUMBER": "row_number"}
_TIES = {"limit": "a plain row limit (ties at the cut are dropped)", "row_number": "ROW_NUMBER (ties at the cut are dropped)", "rank": "RANK (ties kept, ranks skip after a tie)", "dense_rank": "DENSE_RANK (ties kept, no rank skipped)"}


@dataclass
class _Cut:
    method: str  # limit | rank | dense_rank | row_number
    bound: float | None  # None when the bound is a parameter the call did not supply
    text: str


def _judge_rank(
    ctx: CheckContext, term: Term, match: Match, node: exp.Expression, outcome: TermOutcome, params: dict[str, Any] | None
) -> tuple[TermOutcome, Finding | None]:
    """A "top {n}" term: the query must cut its rows the way the catalogue says — by a LIMIT or
    by a bounded ranking function — and at the number the question states."""
    method, bound_text = node.expressions[0].name, node.expressions[1].name
    wanted = bound_number(bound_text)
    if wanted is None:
        value = (params or {}).get(bound_text.strip("{}"))
        wanted = bound_number(str(value)) if value is not None else None
    if wanted is None:
        outcome.detail = f"the term's bound {bound_text} is not bound by the question or the parameters"
        return outcome, None
    expected = f"{_TIES[method]} at {int(wanted)}"
    cuts = _cuts(ctx, params)
    if not cuts:
        outcome.status = MISSING
        outcome.detail = "no LIMIT and no bounded ranking function"
        return outcome, Finding(
            "term_rank",
            Severity.HIGH,
            f'The question asks for "{match.phrase}" ({term.name}), which the catalogue defines as {expected}; '
            "the query has no LIMIT and bounds no ranking function, so it returns every row.",
            "question",
            evidence={"term": term.name, "expected": expected},
        )
    equivalent = {"limit": {"limit", "row_number"}, "row_number": {"limit", "row_number"}, "rank": {"rank"}, "dense_rank": {"dense_rank"}}[method]
    for cut in cuts:
        if cut.bound is None:
            continue
        if cut.method in equivalent and cut.bound == wanted:
            continue
        outcome.status = CONTRADICTED
        outcome.detail = cut.text
        if cut.method in equivalent:
            what = f"the query cuts at {int(cut.bound)} ({cut.text})"
            suggestion = f"Cut at {int(wanted)}."
        else:
            what = f"the query uses {_TIES[cut.method]} ({cut.text})"
            suggestion = f"Cut with {_TIES[method].split(' (')[0]} at {int(wanted)}."
        return outcome, Finding(
            "term_rank",
            Severity.HIGH,
            f'The catalogue defines "{match.phrase}" ({term.name}) as {expected}; {what}.',
            "question",
            snippet=cut.text,
            suggestion=suggestion,
            evidence={"term": term.name, "expected": expected, "found": cut.text},
        )
    if any(cut.bound == wanted and cut.method in equivalent for cut in cuts):
        outcome.status = SATISFIED
        outcome.detail = next(c.text for c in cuts if c.bound == wanted and c.method in equivalent)
        return outcome, None
    outcome.detail = cuts[0].text  # bounded by a parameter the call did not supply
    return outcome, None


def _cuts(ctx: CheckContext, params: dict[str, Any] | None) -> list[_Cut]:
    """Every way the query cuts its rows: LIMIT / FETCH clauses and comparisons that bound a
    ranking function (directly, or through a column a derived source defines as one)."""
    found: list[_Cut] = []
    for model in ctx.cache.models():
        select = model.select
        for key in ("limit", "fetch"):
            clause = select.args.get(key)
            if clause is None:
                continue
            value = clause.args.get("count") if key == "fetch" else clause.expression
            found.append(_Cut("limit", _constant_or_param(model, value, params), clause.sql()))
        parts: list[exp.Expression] = [c.expr for c in model.conjuncts if c.origin == "where"]
        for key in ("qualify", "having"):
            clause = select.args.get(key)
            if clause is not None:
                parts.append(clause.this)
        for part in parts:
            for condition in flatten_and(part):
                cut = _rank_cut(ctx, model, condition, params)
                if cut is not None:
                    found.append(cut)
    return found


def _rank_cut(ctx: CheckContext, model: SelectModel, condition: exp.Expression, params: dict[str, Any] | None) -> _Cut | None:
    condition = strip(condition)
    if isinstance(condition, (exp.LTE, exp.LT, exp.EQ, exp.GTE, exp.GT)):
        pairs = [(condition.this, condition.expression, type(condition))]
        flipped = {exp.LTE: exp.GTE, exp.LT: exp.GT, exp.GTE: exp.LTE, exp.GT: exp.LT, exp.EQ: exp.EQ}
        pairs.append((condition.expression, condition.this, flipped[type(condition)]))
        for side, other, op in pairs:
            method = _rank_function(ctx, model, side)
            if method is None:
                continue
            value = _constant_or_param(model, other, params)
            if op is exp.LT and value is not None:
                value -= 1
            if op in (exp.GTE, exp.GT):
                return None  # a lower bound ("rank > 3") is not a top-N cut
            return _Cut(method, value, condition.sql())
    if isinstance(condition, exp.Between):
        method = _rank_function(ctx, model, condition.this)
        if method is not None:
            return _Cut(method, _constant_or_param(model, condition.args["high"], params), condition.sql())
    if isinstance(condition, exp.In) and condition.expressions:
        method = _rank_function(ctx, model, condition.this)
        if method is not None:
            values = [_constant_or_param(model, e, params) for e in condition.expressions]
            return _Cut(method, max(values) if all(v is not None for v in values) else None, condition.sql())
    return None


def _rank_function(ctx: CheckContext, model: SelectModel | None, node: exp.Expression) -> str | None:
    """``rank`` / ``dense_rank`` / ``row_number`` when ``node`` is that window function, or a
    column that a derived source (a CTE, a subquery) defines as one."""
    node = strip(node)
    if isinstance(node, exp.Window):
        return _RANK_FUNCTIONS.get(node.this.sql_name().upper()) if isinstance(node.this, exp.Func) else None
    if isinstance(node, exp.Column) and model is not None:
        sources = [model.sources[node.table]] if node.table in model.sources else ([] if node.table else list(model.sources.values()))
        for source in sources:
            if source.kind != "derived" or not isinstance(source.node, exp.Select):
                continue
            for projection in source.node.expressions:
                if projection.alias_or_name.lower() == node.name.lower():
                    inner = ctx.cache.model(source.scope) if source.scope is not None else None
                    return _rank_function(ctx, inner, unalias(projection))
    return None


def _constant_or_param(model: SelectModel | None, node: exp.Expression | None, params: dict[str, Any] | None) -> float | None:
    if node is None:
        return None
    value = _constant_of(model, node)
    if value is not None:
        return value
    name = None
    if isinstance(node, exp.Placeholder):
        name = node.name or node.this
    elif isinstance(node, exp.Column) and node.name.startswith("sqlgami_ph_"):
        name = node.name[len("sqlgami_ph_"):]
    elif isinstance(node, exp.Parameter):
        name = node.name
    if name and params and name in params:
        return bound_number(str(params[name]))
    return None


# ------------------------------------------------------------------- source


def _judge_source(ctx: CheckContext, term: Term, match: Match, node: exp.Expression, outcome: TermOutcome) -> tuple[TermOutcome, Finding | None]:
    if not isinstance(node, exp.Column) or not node.table:
        return outcome, None
    column, table = node.name.lower(), node.table.lower()
    elsewhere: list[str] = []
    for model in ctx.cache.models():
        for ref in model.select.find_all(exp.Column):
            if ref.name.lower() != column or ref.table not in model.sources:
                continue
            source = model.sources[ref.table]
            if source.kind != "table" or source.table is None:
                continue
            names = {source.table.name.lower(), source.table.qualified_name.lower()}
            if table not in names and table.split(".")[-1] not in names:
                elsewhere.append(source.table.qualified_name)
    if elsewhere:
        outcome.status = CONTRADICTED
        outcome.detail = f"{column} read from {sorted(set(elsewhere))[0]}"
        return outcome, Finding(
            "term_source",
            Severity.MEDIUM,
            f'The catalogue says "{match.phrase}" ({term.name}) comes from {term.sql}; the query reads '
            f"{column} from {', '.join(sorted(set(elsewhere)))}, which may hold a stale or partial value.",
            "question",
            suggestion=f"Read {column} from {term.sql.rsplit('.', 1)[0]}.",
            evidence={"term": term.name, "expected": term.sql, "found": sorted(set(elsewhere))},
        )
    outcome.status = SATISFIED
    return outcome, None


# ------------------------------------------------------------ formulas / KPIs

_PRIOR_WORDS = re.compile(r"(prior|previous|prev|last|before|old|start|begin|py|ly|yoy_base|anchor)", re.I)
_CURRENT_WORDS = re.compile(r"(current|cur|now|this|new|latest|end|cy|today)", re.I)


def _projections_for(ctx: CheckContext, name: str, phrases: tuple[str, ...]) -> list[exp.Expression]:
    """Root projections whose alias shares a word with the term's name or phrases."""
    root = ctx.analysis.root_scope
    if root is None or not isinstance(root.expression, exp.Select):
        return []
    words = set(re.split(r"[\s_]+", name.lower())) | {w for p in phrases for w in re.split(r"[\s_]+", p.lower()) if "{" not in w}
    words = {w for w in words if len(w) >= 2}
    found = []
    for projection in root.expression.expressions:
        alias = (projection.alias_or_name or "").lower()
        tokens = set(re.split(r"[\s_]+", alias))
        if alias and tokens & words:
            found.append(projection)
    return found


def _integer_shaped(node: exp.Expression) -> bool:
    node = strip(unalias(node))
    if isinstance(node, exp.Cast):
        return "INT" in str(node.to).upper()
    if isinstance(node, (exp.Floor, exp.Ceil)):
        return True
    if isinstance(node, exp.Round):
        decimals = node.args.get("decimals")
        return decimals is None or _number(decimals) == 0
    if isinstance(node, exp.Case):
        branches = [branch.args["true"] for branch in (node.args.get("ifs") or [])]
        default = node.args.get("default")
        outputs = [b for b in branches + ([default] if default is not None else []) if not isinstance(strip(b), exp.Null)]
        return bool(outputs) and all(_integer_shaped(b) for b in outputs)
    if isinstance(node, exp.Count):
        return True
    return False


def _has_scaling(node: exp.Expression) -> bool:
    return any(isinstance(n, (exp.Div, exp.Mul)) for n in walk_own(node))


def _judge_formula(ctx: CheckContext, term: Term, match: Match, node: exp.Expression, outcome: TermOutcome) -> tuple[TermOutcome, Finding | None]:
    return _judge_shape(ctx, term.name, term.phrases, match, node, "integer" if _integer_shaped(node) else "", outcome)


def _judge_kpi(ctx: CheckContext, kpi: KPI, match: Match, catalogue: Catalogue, dialect: str) -> tuple[TermOutcome, Finding | None]:
    outcome = TermOutcome(kpi.name, "kpi", UNVERIFIED, phrase=match.phrase, bindings=match.bindings)
    if not kpi.enforceable:
        outcome.detail = "the catalogue describes this KPI but gives no formula or format"
        return outcome, Finding(
            "undefined_term",
            Severity.LOW,
            f'The question uses "{match.phrase}" ({kpi.name}), which the catalogue lists without a formula or format, '
            "so sqlgami cannot confirm the query computes it correctly.",
            "question",
            evidence={"term": kpi.name, "phrase": match.phrase},
        )
    node = None
    if kpi.formula.strip():
        try:
            from .terms import _placeholders_to_columns
            import sqlglot

            node = sqlglot.parse_one(f"SELECT {_placeholders_to_columns(kpi.formula)} AS v FROM t", read=dialect).expressions[0].this
        except Exception:
            node = None
    fmt = kpi.format.lower()
    wants_integer = "integer" in fmt or "int" in fmt.split() or "bp" in fmt or "floor" in fmt
    return _judge_shape(ctx, kpi.name, kpi.phrases, match, node, "integer" if wants_integer else "", outcome, floored="floor" in fmt or (node is not None and any(isinstance(n, exp.Floor) for n in node.walk())))


def _judge_shape(
    ctx: CheckContext,
    name: str,
    phrases: tuple[str, ...],
    match: Match,
    node: exp.Expression | None,
    unit: str,
    outcome: TermOutcome,
    floored: bool = False,
) -> tuple[TermOutcome, Finding | None]:
    projections = _projections_for(ctx, name, phrases)
    if not projections:
        outcome.detail = "no output column is named for this term"
        return outcome, None
    for projection in projections:
        value = unalias(projection)
        if unit == "integer" and _has_scaling(value) and not _integer_shaped(value):
            outcome.status = CONTRADICTED
            outcome.detail = projection.sql()
            return outcome, Finding(
                "term_format",
                Severity.MEDIUM,
                f'The catalogue says "{match.phrase}" ({name}) is an integer{" (floored)" if floored else ""}; '
                f"the output column {projection.alias_or_name} is computed without casting or flooring to an integer, "
                "so it will carry decimals.",
                "question",
                snippet=ctx.snippet(projection),
                suggestion="Wrap the value in FLOOR(...) or CAST(... AS BIGINT) as the catalogue defines.",
                evidence={"term": name, "column": projection.alias_or_name},
            )
        if floored and any(isinstance(n, exp.Round) for n in walk_own(value)) and not any(isinstance(n, exp.Floor) for n in walk_own(value)):
            outcome.status = CONTRADICTED
            outcome.detail = projection.sql()
            return outcome, Finding(
                "term_format",
                Severity.MEDIUM,
                f'The catalogue says "{match.phrase}" ({name}) is floored, not rounded; {projection.alias_or_name} uses ROUND.',
                "question",
                snippet=ctx.snippet(projection),
                evidence={"term": name, "column": projection.alias_or_name},
            )
        direction = _direction_problem(node, value)
        if direction is not None:
            outcome.status = CONTRADICTED
            outcome.detail = projection.sql()
            return outcome, Finding(
                "term_formula",
                Severity.MEDIUM,
                f'The catalogue defines "{match.phrase}" ({name}) as {direction[0]}; {projection.alias_or_name} subtracts the other way round ({direction[1]}).',
                "question",
                snippet=ctx.snippet(projection),
                evidence={"term": name, "column": projection.alias_or_name},
            )
    outcome.status = SATISFIED
    outcome.detail = projections[0].alias_or_name or ""
    return outcome, None


def _role(node: exp.Expression) -> str | None:
    names = " ".join(f"{n.table} {n.name}" for n in node.find_all(exp.Column)) + " " + (node.alias_or_name or "")
    prior, current = bool(_PRIOR_WORDS.search(names)), bool(_CURRENT_WORDS.search(names))
    if prior and not current:
        return "prior"
    if current and not prior:
        return "current"
    return None


def _direction_problem(formula: exp.Expression | None, value: exp.Expression) -> tuple[str, str] | None:
    """The formula subtracts current − prior (by the names of its placeholders) and the value
    subtracts prior − current (by the names of its operands)."""
    if formula is None:
        return None
    sub = next((n for n in formula.walk() if isinstance(n, exp.Sub)), None)
    if sub is None:
        return None
    left_role, right_role = _role(sub.this), _role(sub.expression)
    if not left_role or not right_role or left_role == right_role:
        return None
    for candidate in walk_own(value):
        if not isinstance(candidate, exp.Sub):
            continue
        l, r = _role(candidate.this), _role(candidate.expression)
        if l and r and l != r and (l, r) != (left_role, right_role):
            return (f"{left_role} minus {right_role}", candidate.sql())
    return None
