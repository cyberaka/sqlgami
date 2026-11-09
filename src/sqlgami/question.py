"""Compare the plain-English question with what the SQL actually does.

These checks are deterministic word-and-structure matching, with no language
model involved. They look for things the question asks for that the SQL lacks:
a value it names, a number, a "top 5", an average, a "per customer", a "not",
a time period. They are heuristics, so they report at medium or low severity
unless the structure of the query confirms the problem.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from sqlglot import exp

from . import types as T
from .checks.base import CheckContext
from .model import NO, YES, SelectModel, strip, walk_own
from .report import Finding, Severity

STAGE = "question"

_GENERIC_VALUES = {"all", "none", "other", "true", "false", "yes", "no", "null", "n/a", "na", "unknown", "any"}

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "twenty": 20, "fifty": 50, "hundred": 100,
}
_NUM = r"(\d+|" + "|".join(_NUMBER_WORDS) + r")"

_DESC_WORDS = r"top|largest|highest|biggest|most|best|greatest|maximum|max|latest|newest|most recent"
_ASC_WORDS = r"bottom|smallest|lowest|least|worst|fewest|minimum|min|earliest|oldest|cheapest"
_TOP_N = re.compile(
    rf"\b(?:(?P<w1>{_DESC_WORDS}|{_ASC_WORDS}|first|last)\s+{_NUM}\b|{_NUM}\s+(?P<w2>{_DESC_WORDS}|{_ASC_WORDS})\b)",
    re.IGNORECASE,
)
_SUPERLATIVE = re.compile(rf"\b({_DESC_WORDS}|{_ASC_WORDS})\b", re.IGNORECASE)
_STRONG_DIRECTION = {
    "top", "largest", "highest", "biggest", "most", "greatest", "maximum", "max",
    "bottom", "smallest", "lowest", "least", "fewest", "minimum", "min",
}

_COUNT_PHRASE = re.compile(r"\b(how many|number of|count of|count)\b", re.IGNORECASE)
_AVG_WORD = re.compile(r"\b(average|avg|mean)\b", re.IGNORECASE)
_MEDIAN_WORD = re.compile(r"\bmedian\b", re.IGNORECASE)
_GROUP_PHRASE = re.compile(
    r"\b(per|for each|for every|broken down by|breakdown by|grouped by|split by|"
    r"by (?:month|year|week|day|quarter|hour))\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(not|never|without|excluding|except|exclude|other than|neither|isn't|aren't|don't|doesn't|"
    r"haven't|hasn't|didn't|wasn't|weren't|no longer)\b|\bnon-",
    re.IGNORECASE,
)
_MONTHS = "january|february|march|april|june|july|august|september|october|november|december"
_TIME_PHRASE = re.compile(
    rf"\b(today|yesterday|tomorrow|(?:this|last|previous|past|next|current)\s+(?:\d+\s+)?"
    rf"(?:day|week|month|quarter|year)s?|year to date|ytd|month to date|mtd|"
    rf"in the (?:last|past)\s+\d+\s+(?:day|week|month|year)s?|(?:{_MONTHS})|(?:19|20)\d{{2}})\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_QUESTION_NUMBER = re.compile(r"(?<![\w.,-])\$?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w-]|\.\d)")
_TEMPORAL_NAME = re.compile(r"(_at|_date|_time|_ts|_on|_day|_month|_year|date|timestamp|effective_|valid_|period)", re.IGNORECASE)
_SCALES = (1, 100, 1_000, 10_000, 1_000_000, 100_000_000)


def run_question_checks(ctx: CheckContext, question: str, glossary: str | None = None) -> list[Finding]:
    """Every question-versus-SQL check. Never raises for an odd question; returns what it can."""
    text = " ".join((question or "").split())
    if not text or ctx.analysis.resolved is None:
        return []
    asked = _Asked(ctx, text, glossary or "")
    findings: list[Finding] = []
    for check in (
        _values_named_but_unused,
        _filters_not_asked_for,
        _count_grain,
        _top_n,
        _aggregates,
        _numbers,
        _grouping,
        _negation,
        _time_period,
    ):
        try:
            findings.extend(check(asked) or [])
        except Exception:  # a heuristic must never break scoring
            continue
    return findings


class _Asked:
    """The question, the SQL, and a few facts about each that several checks share."""

    def __init__(self, ctx: CheckContext, text: str, glossary: str) -> None:
        self.ctx = ctx
        self.text = text
        self.lower = text.lower()
        self.context_lower = (text + " " + glossary).lower()
        self.tree: exp.Expression = ctx.analysis.resolved
        root = ctx.analysis.root_scope
        self.root: SelectModel | None = None
        if root is not None and isinstance(root.expression, exp.Select):
            self.root = ctx.cache.model(root)
        self.string_literals = [n.name for n in self.tree.find_all(exp.Literal) if n.is_string]
        self.string_literals_lower = {s.strip().lower() for s in self.string_literals}
        self.number_literals: list[float] = []
        for node in self.tree.find_all(exp.Literal):
            if not node.is_string:
                try:
                    self.number_literals.append(float(node.name))
                except ValueError:
                    pass
        top = _TOP_N.search(text)
        self.top_word: str | None = None
        self.top_n: int | None = None
        if top:
            self.top_word = (top.group("w1") or top.group("w2") or "").lower()
            raw = next(g for g in top.groups()[1:] if g and (g.isdigit() or g.lower() in _NUMBER_WORDS))
            self.top_n = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw.lower()]

    def has(self, *kinds: type) -> bool:
        return any(True for _ in self.tree.find_all(*kinds))

    def finding(self, code: str, severity: Severity, message: str, suggestion: str | None = None, **evidence: Any) -> Finding:
        return Finding(code, severity, message, STAGE, None, suggestion, evidence)

    def mentions(self, value: str, text: str | None = None) -> bool:
        """Whether the question names ``value`` as a word or phrase."""
        raw = str(value).strip()
        if not raw:
            return False
        haystack = self.lower if text is None else text
        if len(raw) <= 2:
            # 'US', 'IN', 'NO': only a capitalised whole word counts, or "in" would match everywhere.
            if not raw.isupper() or text is not None:
                return False
            return re.search(rf"(?<![A-Za-z0-9]){re.escape(raw)}(?![A-Za-z0-9])", self.text) is not None
        folded = raw.lower()
        forms = {folded, folded.replace("_", " "), folded.replace("-", " "), folded.replace("_", "-")}
        for form in forms:
            if re.search(rf"(?<![a-z0-9]){re.escape(form)}(?:s|es|d|ed)?(?![a-z0-9])", haystack):
                return True
        return False


# ------------------------------------------------------------------- values


def _categorical_columns(asked: _Asked) -> Iterable[tuple[Any, Any]]:
    for table in asked.ctx.analysis.tables.values():
        for column in table.columns:
            if column.values is None:
                continue
            if T.family_of_declared(column.type, asked.ctx.dialect) in (T.NUMERIC, T.DATE, T.TIMESTAMP, T.BOOLEAN):
                continue
            yield table, column


def _column_is_broken_out(asked: _Asked, table: Any, column: Any) -> bool:
    """True when the query groups by or returns the column, i.e. shows every value of it."""
    key = asked.ctx.catalog.storage_key(column.name)
    for model in asked.ctx.cache.models():
        nodes: list[exp.Expression] = list(model.select.expressions)
        if model.group is not None:
            nodes.extend(model.group.expressions)
        for node in nodes:
            for col in walk_own(node):
                if isinstance(col, exp.Column) and col.name == key:
                    base = model.base_column(col)
                    if base is not None and base[0] is table:
                        return True
    return False


def _values_named_but_unused(asked: _Asked) -> list[Finding]:
    findings: list[Finding] = []
    reported: set[str] = set()
    for table, column in _categorical_columns(asked):
        for value in column.values or ():
            if not isinstance(value, str):
                continue
            folded = value.strip().lower()
            if not folded or folded in _GENERIC_VALUES or folded in reported:
                continue
            if not asked.mentions(value):
                continue
            if folded in asked.string_literals_lower:
                continue
            if _column_is_broken_out(asked, table, column):
                continue
            # The same word may be a value of several columns; one use anywhere settles it.
            reported.add(folded)
            findings.append(
                asked.finding(
                    "question_value_unused",
                    Severity.MEDIUM,
                    f"The question mentions {value!r}, which is a value of {table.name}.{column.name}, "
                    "but the SQL never filters on it.",
                    suggestion=f"Check whether {column.name} = {value!r} belongs in the query.",
                    value=value,
                    column=f"{table.name}.{column.name}",
                )
            )
    return findings


def _filters_not_asked_for(asked: _Asked) -> list[Finding]:
    """A filter on a named value that neither the question nor the glossary mentions."""
    ctx = asked.ctx
    policy = {c.lower() for c in (ctx.catalog.tenant_column, ctx.catalog.soft_delete_column) if c}
    findings: list[Finding] = []
    seen: set[str] = set()
    for model in ctx.cache.models():
        for node in walk_own(model.select):
            pairs: list[tuple[exp.Expression, exp.Expression]] = []
            if isinstance(node, exp.EQ):
                pairs = [(node.this, node.expression), (node.expression, node.this)]
            elif isinstance(node, exp.In) and not node.args.get("query") and not isinstance(node.parent, exp.Not):
                pairs = [(node.this, value) for value in node.expressions]
            for left, right in pairs:
                column, literal = strip(left), strip(right)
                if not isinstance(column, exp.Column) or not isinstance(literal, exp.Literal) or not literal.is_string:
                    continue
                if column.table not in model.sources:
                    continue
                value = literal.name.strip()
                folded = value.lower()
                if len(folded) < 3 or folded in seen or column.name.lower() in policy:
                    continue
                if re.match(r"^\d{4}-\d{2}", folded) or _TEMPORAL_NAME.search(column.name):
                    continue
                base = model.base_column(column)
                if asked.mentions(value, asked.context_lower):
                    continue
                if base is None:
                    continue
                # Written inside a view: the view's author chose it, not the model.
                if _inside_view(node):
                    continue
                seen.add(folded)
                findings.append(
                    asked.finding(
                        "filter_not_in_question",
                        Severity.LOW,
                        f"The SQL filters on {base[0].name}.{base[1]} = {value!r}, which the question "
                        "does not mention.",
                        value=value,
                        column=f"{base[0].name}.{base[1]}",
                    )
                )
    return findings


def _inside_view(node: exp.Expression) -> bool:
    current: exp.Expression | None = node
    while current is not None:
        if isinstance(current, exp.Subquery) and current.meta.get("sqlgami_view"):
            return True
        current = current.parent
    return False


# -------------------------------------------------------------------- counts


def _name_forms(table_name: str) -> list[str]:
    """Ways a question might name a table: 'billing_invoice_line_items' -> 'line items', 'items'..."""
    words = [w for w in re.split(r"[_\W]+", table_name.lower()) if w and w not in ("v", "vw", "view", "mv")]
    forms: list[str] = []
    for start in range(len(words)):
        phrase = " ".join(words[start:])
        if phrase and phrase not in forms:
            forms.append(phrase)
    return forms


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ses") or word.endswith("xes"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _entity_position(window: str, table_name: str) -> int | None:
    best: int | None = None
    for form in _name_forms(table_name):
        head, _, last = form.rpartition(" ")
        stem = re.escape(_singular(last))
        prefix = re.escape(head) + r"\s+" if head else ""
        match = re.search(rf"(?<![a-z]){prefix}{stem}(?:s|es|ies)?(?![a-z])", window)
        if match and len(form) >= 3:
            if best is None or match.start() < best:
                best = match.start()
    return best


def _count_grain(asked: _Asked) -> list[Finding]:
    """'How many customers' answered by counting rows of a join that repeats customers."""
    root = asked.root
    match = _COUNT_PHRASE.search(asked.lower)
    if root is None or match is None or len(root.sources) < 2:
        return []
    counts = [
        a for a in root.own_aggregates()
        if isinstance(a, exp.Count) and not isinstance(a.this, exp.Distinct) and not a.args.get("distinct")
    ]
    if not counts:
        return []
    window = asked.lower[match.end(): match.end() + 80]
    best: tuple[int, str] | None = None
    for alias, source in root.sources.items():
        table = _table_behind(root, alias)
        if table is None:
            continue
        position = _entity_position(window, table.name)
        if position is not None and (best is None or position < best[0]):
            best = (position, alias)
    if best is None:
        return []
    alias = best[1]
    multiplied = root.multiplied(alias)
    if multiplied.state == NO:
        return []
    # Grouping by the entity's own key gives one row per entity; then the count is of something else.
    group = root.group
    if group is not None:
        grouped = {strip(e).name for e in group.expressions if isinstance(strip(e), exp.Column) and strip(e).table == alias}
        if any(key and key <= grouped for key in root.key_info(alias).keys):
            return []
    count = counts[0]
    source = root.sources[alias]
    by = ", ".join(root.sources[c].label for c in multiplied.culprits) or "another table"
    confirmed = multiplied.state == YES
    return [
        asked.finding(
            "count_grain",
            Severity.HIGH if confirmed else Severity.MEDIUM,
            f"The question asks how many {source.table.name if source.table else alias} there are, but "
            f"{asked.ctx.snippet(count)} counts rows of the join, and the join to {by} "
            + ("repeats" if confirmed else "may repeat")
            + f" each {source.label} row. The answer counts {by} rows instead.",
            suggestion=f"Count DISTINCT keys of {alias}, or filter with EXISTS instead of joining.",
            entity=alias,
            repeated_by=multiplied.culprits,
            certainty=multiplied.state,
        )
    ]


def _table_behind(model: SelectModel, alias: str) -> Any:
    """The table a source stands for, looking through a single-table derived source or view."""
    source = model.sources[alias]
    if source.table is not None:
        return source.table
    if source.kind == "derived" and source.scope is not None and isinstance(source.scope.expression, exp.Select):
        inner = model.cache.model(source.scope)
        if len(inner.sources) == 1 and not inner.is_aggregated:
            return _table_behind(inner, next(iter(inner.sources)))
    return None


# --------------------------------------------------------------------- top N


def _root_limit(asked: _Asked) -> int | None:
    select = asked.tree
    while isinstance(select, exp.Subquery):
        select = select.this
    limit = select.args.get("limit") if isinstance(select, exp.Query) else None
    if limit is None:
        return None
    value = limit.expression
    if isinstance(value, exp.Literal) and not value.is_string:
        try:
            return int(float(value.name))
        except ValueError:
            return None
    return None


def _rank_filter(asked: _Asked, n: int) -> bool:
    """A ``ROW_NUMBER()/RANK() ... <= n`` style filter anywhere in the query."""
    if not asked.has(exp.RowNumber, exp.Rank, exp.DenseRank):
        return False
    for node in asked.tree.find_all(exp.LTE, exp.LT, exp.EQ, exp.Between):
        for literal in node.find_all(exp.Literal):
            if not literal.is_string and literal.name in (str(n), str(n + 1)):
                return True
    return False


def _top_n(asked: _Asked) -> list[Finding]:
    findings: list[Finding] = []
    limit = _root_limit(asked)
    n = asked.top_n
    if n is not None:
        if limit is None and not _rank_filter(asked, n):
            if not (n == 1 and asked.has(exp.Max, exp.Min, exp.ArgMax, exp.ArgMin)):
                findings.append(
                    asked.finding(
                        "top_n",
                        Severity.MEDIUM,
                        f"The question asks for {asked.top_word} {n}, but the SQL has no LIMIT {n} or ranking filter.",
                        suggestion=f"Add ORDER BY ... LIMIT {n}.",
                        asked=n,
                    )
                )
        elif limit is not None and limit != n and not _rank_filter(asked, n):
            findings.append(
                asked.finding(
                    "top_n",
                    Severity.MEDIUM,
                    f"The question asks for {asked.top_word} {n}, but the SQL limits to {limit} rows.",
                    asked=n,
                    limit=limit,
                )
            )
    words = {m.group(1).lower() for m in _SUPERLATIVE.finditer(asked.text)}
    desc_words = {w for w in words if re.fullmatch(_DESC_WORDS, w)}
    asc_words = {w for w in words if re.fullmatch(_ASC_WORDS, w)}
    select = asked.tree
    order = select.args.get("order") if isinstance(select, exp.Query) else None
    if order is not None and limit is not None and order.expressions and bool(desc_words) != bool(asc_words):
        first = order.expressions[0]
        is_desc = bool(first.args.get("desc"))
        wants_desc = bool(desc_words)
        if is_desc != wants_desc:
            word = sorted(desc_words or asc_words)[0]
            findings.append(
                asked.finding(
                    "order_direction",
                    Severity.MEDIUM if word in _STRONG_DIRECTION else Severity.LOW,
                    f"The question asks for the {word}, but the SQL sorts "
                    f"{'descending' if is_desc else 'ascending'} before taking the first rows.",
                    suggestion="Check the ORDER BY direction.",
                    word=word,
                )
            )
    elif n is None and (desc_words & _STRONG_DIRECTION or asc_words & _STRONG_DIRECTION):
        handled = (
            limit is not None
            or asked.has(exp.Max, exp.Min, exp.ArgMax, exp.ArgMin, exp.RowNumber, exp.Rank, exp.DenseRank)
            or order is not None
        )
        if not handled:
            word = sorted((desc_words | asc_words) & _STRONG_DIRECTION)[0]
            findings.append(
                asked.finding(
                    "superlative_unhandled",
                    Severity.LOW,
                    f"The question asks for the {word}, but the SQL has no ordering, MAX/MIN or ranking.",
                    word=word,
                )
            )
    return findings


# ---------------------------------------------------------------- aggregates


def _aggregates(asked: _Asked) -> list[Finding]:
    findings: list[Finding] = []
    has_avg = asked.has(exp.Avg)
    has_ratio = any(
        any(isinstance(n, exp.AggFunc) for n in div.find_all(exp.AggFunc)) for div in asked.tree.find_all(exp.Div)
    )
    if _AVG_WORD.search(asked.text) and not has_avg and not has_ratio:
        findings.append(
            asked.finding(
                "aggregate_mismatch",
                Severity.MEDIUM,
                "The question asks for an average, but the SQL computes no AVG and no ratio of aggregates.",
            )
        )
    if _MEDIAN_WORD.search(asked.text):
        median_kinds = tuple(
            getattr(exp, n) for n in ("Median", "PercentileCont", "PercentileDisc", "Quantile", "ApproxQuantile")
            if hasattr(exp, n)
        )
        if not asked.has(*median_kinds):
            findings.append(
                asked.finding(
                    "aggregate_mismatch",
                    Severity.MEDIUM,
                    "The question asks for a median, but the SQL computes no median or percentile.",
                )
            )
    if _COUNT_PHRASE.search(asked.text) and not asked.has(exp.Count, exp.Sum, exp.CountIf):
        findings.append(
            asked.finding(
                "aggregate_mismatch",
                Severity.MEDIUM,
                "The question asks how many, but the SQL counts and sums nothing.",
            )
        )
    return findings


# ------------------------------------------------------------------- numbers


def _numbers(asked: _Asked) -> list[Finding]:
    findings: list[Finding] = []
    literal_text = " ".join(asked.string_literals)
    numeric = set(asked.number_literals)
    for match in _QUESTION_NUMBER.finditer(asked.text):
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if _YEAR.fullmatch(raw):
            if raw in literal_text or value in numeric or any(raw in str(int(n)) for n in numeric if n >= 10000):
                continue
            findings.append(
                asked.finding(
                    "time_filter_missing",
                    Severity.MEDIUM,
                    f"The question mentions {raw}, but that year appears nowhere in the SQL.",
                    year=raw,
                )
            )
            continue
        if asked.top_n is not None and value == asked.top_n:
            continue
        if any(value * scale in numeric for scale in _SCALES) or value / 100 in numeric:
            continue
        if re.search(rf"(?<!\d){re.escape(raw)}(?!\d)", literal_text):
            continue
        before = asked.lower[max(0, match.start() - 12): match.start()]
        if re.search(r"(tenant|id|#|no\.|number)\s*$", before) and value in numeric:
            continue
        findings.append(
            asked.finding(
                "number_unused",
                Severity.MEDIUM,
                f"The question mentions the number {match.group(1)}, but it appears nowhere in the SQL.",
                number=match.group(1),
            )
        )
    return findings


# ------------------------------------------------------------------ grouping


def _grouping(asked: _Asked) -> list[Finding]:
    match = _GROUP_PHRASE.search(asked.text)
    if match is None:
        return []
    if asked.has(exp.Group, exp.Window) or any(
        s.args.get("distinct") is not None for s in asked.tree.find_all(exp.Select)
    ):
        return []
    root = asked.root
    if root is None or not root.own_aggregates():
        return []
    return [
        asked.finding(
            "grouping_missing",
            Severity.MEDIUM,
            f"The question asks for a figure \"{match.group(0)}\" something, but the SQL returns a "
            "single aggregate with no GROUP BY.",
            phrase=match.group(0),
        )
    ]


# ------------------------------------------------------------------ negation


def _negation(asked: _Asked) -> list[Finding]:
    match = _NEGATION.search(asked.text)
    if match is None:
        return []
    if asked.has(exp.Not, exp.NEQ, exp.Except, exp.Is):
        return []
    if any((j.kind or "").upper() == "ANTI" for j in asked.tree.find_all(exp.Join)):
        return []
    return [
        asked.finding(
            "negation_missing",
            Severity.MEDIUM,
            f"The question says \"{match.group(0).strip()}\", but the SQL has no NOT, <>, IS NULL, "
            "NOT IN, NOT EXISTS or EXCEPT.",
            phrase=match.group(0).strip(),
        )
    ]


# ---------------------------------------------------------------------- time


def _is_temporal_column(asked: _Asked, model: SelectModel, column: exp.Column) -> bool:
    if column.table not in model.sources:
        return False
    base = model.base_column(column)
    if base is not None:
        declared = asked.ctx.catalog.column(base[0], base[1], quoted=True)
        if declared is not None and T.family_of_declared(declared.type, asked.ctx.dialect) in T.TEMPORAL:
            return True
        return bool(_TEMPORAL_NAME.search(base[1]))
    return bool(_TEMPORAL_NAME.search(column.name))


def _time_period(asked: _Asked) -> list[Finding]:
    match = _TIME_PHRASE.search(asked.text)
    if match is None:
        return []
    kinds = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In, exp.Is, exp.Like)
    for model in asked.ctx.cache.models():
        for node in walk_own(model.select):
            if not isinstance(node, kinds):
                continue
            if any(isinstance(c, exp.Column) and _is_temporal_column(asked, model, c) for c in walk_own(node)):
                return []
        # Grouping or selecting by a time bucket also answers "by month" style questions.
        if model.group is not None:
            for expr in model.group.expressions:
                if any(isinstance(c, exp.Column) and _is_temporal_column(asked, model, c) for c in walk_own(expr)):
                    return []
    if _YEAR.fullmatch(match.group(0)):
        return []  # reported by the number check with the year named
    return [
        asked.finding(
            "time_filter_missing",
            Severity.MEDIUM,
            f"The question refers to a time period (\"{match.group(0)}\"), but the SQL has no "
            "condition on any date or time column.",
            phrase=match.group(0),
        )
    ]


__all__ = ["run_question_checks", "STAGE"]
