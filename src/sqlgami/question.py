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
from .checks.policy import inside_view
from .model import NO, YES, SelectModel, strip, walk_own
from .report import Finding, Severity

STAGE = "question"

LONG_QUESTION_WORDS = 45
_WORD_MATCHING = {
    "question_value_unused",
    "number_unused",
    "negation_missing",
    "aggregate_mismatch",
    "output_columns",
    "grouping_missing",
    "time_filter_missing",
    "pattern_match_missing",
    "question_flag_unused",
    "superlative_unhandled",
}

_NEGATED_BEFORE = re.compile(r"\b(not|no|non|excluding|except|without|exclude|other than|never)\W*$", re.IGNORECASE)
_PARENTHETICAL = re.compile(r"\([^()]*\)")

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

_COUNT_PHRASE = re.compile(
    r"\b(how many|number of(?!\s+(?:days|hours|minutes|seconds|weeks|months|years|periods)\b)|count of|count)\b",
    re.IGNORECASE,
)
_TOTAL_WORD = re.compile(r"\b(total|sum)\b(?!\s+(?:number|count)\b)", re.IGNORECASE)
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
_SCALES = (1, 10, 100, 1_000, 10_000, 1_000_000, 100_000_000)


def run_question_checks(ctx: CheckContext, question: str, glossary: str | None = None) -> list[Finding]:
    """Every question-versus-SQL check. Never raises for an odd question; returns what it can."""
    text = " ".join((question or "").split())
    if not text or ctx.analysis.resolved is None:
        return []
    asked = _Asked(ctx, text, glossary or "")
    findings: list[Finding] = []
    long_question = len(text.split()) > LONG_QUESTION_WORDS
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
        _comparison_words,
        _flags_named_but_unused,
        _pattern_match,
        _sorting,
        _output_columns,
    ):
        try:
            found = check(asked) or []
        except Exception:  # a heuristic must never break scoring
            continue
        for finding in found:
            if long_question and finding.code in _WORD_MATCHING and finding.severity == Severity.MEDIUM:
                # Matching words between a long specification and the SQL goes wrong often
                # enough that these are notes, not doubts, on such questions.
                finding.severity = Severity.LOW
                finding.evidence["long_question"] = True
        findings.extend(found)
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
        top = None
        for candidate in _TOP_N.finditer(text):
            before = text[max(0, candidate.start() - 4): candidate.start()].lower()
            if before.endswith("at ") or before.endswith("the "):
                # "at least one", "at most 5": a bound, not a ranking. "the first 5" stays a ranking.
                if before.endswith("at "):
                    continue
            top = candidate
            break
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
            return re.search(rf"(?<![A-Za-z0-9_]){re.escape(raw)}(?![A-Za-z0-9_])", self.text) is not None
        folded = raw.lower()
        forms = {folded, folded.replace("_", " "), folded.replace("-", " "), folded.replace("_", "-")}
        for form in forms:
            for match in re.finditer(rf"(?<![a-z0-9_]){re.escape(form)}(?:s|es|d|ed)?(?![a-z0-9_])", haystack):
                if text is None and _NEGATED_BEFORE.search(haystack[max(0, match.start() - 14): match.start()]):
                    continue  # "not 'partial'", "excluding refunded": named in order to rule it out
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


def _names_value_pointedly(asked: _Asked, value: str, table_name: str) -> bool:
    """Whether the question uses a stored value as a filter: quoted, or right before its table's noun.

    "paid invoices" and "status='paid'" are filters. "negative for credits" and
    "country + its regions" merely contain words that also happen to be stored values.
    """
    raw = str(value).strip()
    if re.search(rf"['\"`]\s*{re.escape(raw)}\s*['\"`]", asked.text, re.IGNORECASE):
        return True
    if len(raw) <= 2:
        return True  # short codes are matched case-sensitively already
    folded = raw.lower()
    forms = {folded, folded.replace("_", " "), folded.replace("-", " ")}
    for noun in _name_forms(table_name):
        head, _, last = noun.rpartition(" ")
        stem = re.escape(_singular(last))
        noun_pattern = (re.escape(head) + r"\s+" if head else "") + stem + r"(?:s|es|ies)?"
        for form in forms:
            if re.search(rf"(?<![a-z0-9_]){re.escape(form)}\s+(?:[a-z-]+\s+)?{noun_pattern}(?![a-z])", asked.lower):
                return True
    return False


def _column_is_broken_out(asked: _Asked, table: Any, column: Any) -> bool:
    """True when the query groups by or returns the column, i.e. shows every value of it."""
    key = asked.ctx.catalog.column_key(column.name)
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
            if not _names_value_pointedly(asked, value, table.name):
                continue
            if folded in asked.string_literals_lower:
                continue
            if len(folded) >= 3 and any(folded in literal for literal in asked.string_literals_lower):
                continue  # used inside a pattern such as 'pro%'
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
                if inside_view(node):
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
    multiplied = root.multiplied(alias, per_group=True)
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
    words = {
        m.group(1).lower()
        for m in _SUPERLATIVE.finditer(asked.text)
        if not asked.text[max(0, m.start() - 3): m.start()].lower().endswith("at ")
    }
    desc_words = {w for w in words if re.fullmatch(_DESC_WORDS, w)}
    asc_words = {w for w in words if re.fullmatch(_ASC_WORDS, w)}
    select = asked.tree
    order = select.args.get("order") if isinstance(select, exp.Query) else None
    answered_by_aggregate = (
        (desc_words and asked.has(exp.Max, exp.ArgMax))
        or (asc_words and asked.has(exp.Min, exp.ArgMin))
        or asked.has(exp.RowNumber, exp.Rank, exp.DenseRank)  # ORDER BY position: the window holds the direction
    )
    if (
        order is not None
        and limit is not None
        and order.expressions
        and bool(desc_words) != bool(asc_words)
        and not answered_by_aggregate
    ):
        first = order.expressions[0]
        is_desc = bool(first.args.get("desc"))
        wants_desc = bool(desc_words)
        if is_desc != wants_desc:
            word = sorted(desc_words or asc_words)[0]
            findings.append(
                asked.finding(
                    "order_direction",
                    Severity.HIGH if word in _STRONG_DIRECTION else Severity.LOW,
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
    total = _TOTAL_WORD.search(asked.text)
    if total and not asked.has(exp.Sum) and asked.has(exp.Count) and not _COUNT_PHRASE.search(asked.text):
        findings.append(
            asked.finding(
                "aggregate_mismatch",
                Severity.MEDIUM,
                f"The question asks for a {total.group(1).lower()}, but the SQL counts rows and sums nothing.",
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
        if value == 0:
            continue
        if value < 100 and _bound_phrase(asked, match.start(), match.end()) is None:
            continue  # "(1 = highest)", "2 = next", "top 3": small numbers that are not bounds
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


_NO_RELATED = re.compile(
    r"\b(?:have|has|had|with|having|contain|contains|containing)\s+no\b|\bnever\b|\bwithout\b|"
    r"\bno\s+(?:associated|matching|corresponding|related|linked)\b|"
    r"\b(?:do|does|did)\s*(?:not|n't)\s+have\b|\bnot\s+(?:have|having)\b",
    re.IGNORECASE,
)
_NOT_A_NEGATION = re.compile(
    r"\b(?:with|has|have)\s+no\s+(?:duplicates?|ties|particular|specific)\b|\bwithout\s+duplicates?\b|"
    r"\b(?:appear|appears|included|shown|listed|returned)\b[^.]*\b(?:0|zero)\b|\b(?:0|zero)\s+(?:when|if|for)\b|"
    r"\binclud\w*\b[^.]*\bwith\s+no\b|\bwith\s+no\s+\w+(?:\s+\w+)?\s+(?:get|gets|have|show|return|appear)s?\b|"
    r"\bnot\s+applied\b|\bno\s+(?:float|rounding|limit|ordering|filter|deleted_at)\b|\bnull\s+if\s+none\b",
    re.IGNORECASE,
)


def _has_anti_pattern(asked: _Asked) -> bool:
    """NOT EXISTS, NOT IN, EXCEPT, an anti-join, or a zero-count test."""
    for node in asked.tree.find_all(exp.Not):
        inner = node.this
        while isinstance(inner, exp.Paren):
            inner = inner.this
        if isinstance(inner, (exp.Exists, exp.In)):
            return True
    if asked.has(exp.Except):
        return True
    if any((j.kind or "").upper() == "ANTI" for j in asked.tree.find_all(exp.Join)):
        return True
    for model in asked.ctx.cache.models():
        for conjunct in model.conjuncts:
            expr = conjunct.expr
            if isinstance(expr, exp.Is) and isinstance(expr.expression, exp.Null):
                column = strip(expr.this)
                if isinstance(column, exp.Column) and column.table in model.sources:
                    if model.sources[column.table].null_supplied:
                        return True  # LEFT JOIN ... WHERE other.key IS NULL
    for node in asked.tree.find_all(exp.EQ, exp.LTE, exp.LT):
        sides = (strip(node.this), strip(node.expression))
        if any(isinstance(side, exp.Literal) and side.name in ("0", "1") for side in sides) and any(
            isinstance(side, (exp.Count, exp.Sum, exp.Coalesce, exp.Column, exp.Subquery)) for side in sides
        ):
            if any(n for n in node.find_all(exp.Count)) or isinstance(node, exp.EQ):
                if any(isinstance(side, exp.Literal) and side.name == "0" for side in sides):
                    return True
    return False


def _negation(asked: _Asked) -> list[Finding]:
    # Asides in parentheses explain; they are not part of what is being asked for.
    main_text = " ".join(_PARENTHETICAL.sub(" ", asked.text).split())
    main_text = re.sub(r"\b(?:is\s+)?not\s+null\b", " ", main_text, flags=re.IGNORECASE)
    related = _NO_RELATED.search(main_text)
    if related is not None and _NOT_A_NEGATION.search(main_text):
        related = None
        if not _NEGATION.search(_NOT_A_NEGATION.sub(" ", main_text)):
            return []
    if related is not None:
        if _has_anti_pattern(asked):
            return []
        # "no ended_at", "no email": a missing value, which IS NULL answers.
        if asked.has(exp.Is):
            tail = main_text.lower()[related.end(): related.end() + 40]
            column_names = {c.name.lower() for t in asked.ctx.analysis.tables.values() for c in t.columns}
            words = re.findall(r"[a-z_]+", tail)
            if any(w in column_names or w.replace(" ", "_") in column_names for w in words[:3]):
                return []
        return [
            asked.finding(
                "negation_missing",
                Severity.MEDIUM,
                f"The question asks for things that lack something (\"{related.group(0).strip()}\"), but "
                "the SQL has no NOT EXISTS, NOT IN, EXCEPT, anti-join or zero-count test.",
                phrase=related.group(0).strip(),
            )
        ]
    match = _NEGATION.search(main_text)
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


# ---------------------------------------------------------- comparison words

_STRICT_ABOVE = r"greater than|more than|over|above|exceeds?|exceeding|strictly after|after|later than|higher than|bigger than|larger than"
_INCLUSIVE_ABOVE = r"at least|no less than|not less than|on or after|or more|or greater|or later|minimum of|from"
_STRICT_BELOW = r"less than|fewer than|under|below|strictly before|before|earlier than|lower than|smaller than"
_INCLUSIVE_BELOW = r"at most|no more than|not more than|on or before|or less|or fewer|or earlier|up to|maximum of|through|until"
_BOUND_WORDS = re.compile(
    rf"\b(?:(?P<sa>{_STRICT_ABOVE})|(?P<ia>{_INCLUSIVE_ABOVE})|(?P<sb>{_STRICT_BELOW})|(?P<ib>{_INCLUSIVE_BELOW}))\b",
    re.IGNORECASE,
)
_DATE_IN_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}")


def _bound_phrase(asked: _Asked, position: int, end: int) -> str | None:
    """The comparison wording attached to a number at ``position``: 'sa', 'ia', 'sb', 'ib' or None."""
    before = asked.text[max(0, position - 28): position]
    after = asked.text[end: end + 14]
    found = None
    for match in _BOUND_WORDS.finditer(before):
        gap = before[match.end():]
        if len(gap.split()) <= 2 and match.lastgroup not in ("ia", "ib") or len(gap.split()) <= 2:
            found = match.lastgroup
    trailing = re.match(r"\s*(or more|or greater|or later|or less|or fewer|or earlier)\b", after, re.IGNORECASE)
    if trailing:
        found = "ia" if trailing.group(1).lower() in ("or more", "or greater", "or later") else "ib"
    return found


def _comparison_words(asked: _Asked) -> list[Finding]:
    """'greater than 50000' written as >= 50000, and the like."""
    findings: list[Finding] = []
    mentions: list[tuple[str, str]] = []  # (literal text as it would appear in SQL, phrase kind)
    for match in _QUESTION_NUMBER.finditer(asked.text):
        kind = _bound_phrase(asked, match.start(), match.end())
        if kind:
            mentions.append((match.group(1).replace(",", ""), kind))
    for match in _DATE_IN_TEXT.finditer(asked.text):
        kind = _bound_phrase(asked, match.start(), match.end())
        if kind:
            mentions.append((match.group(0), kind))
    if not mentions:
        return findings
    operators = {exp.GT: "sa", exp.GTE: "ia", exp.LT: "sb", exp.LTE: "ib"}
    flipped = {"sa": "sb", "ia": "ib", "sb": "sa", "ib": "ia"}
    symbol = {"sa": ">", "ia": ">=", "sb": "<", "ib": "<="}
    seen: set[str] = set()
    for node in asked.tree.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE):
        left, right = strip(node.this), strip(node.expression)
        used = operators[type(node)]
        literal = right if isinstance(right, exp.Literal) else left if isinstance(left, exp.Literal) else None
        if literal is None:
            continue
        if literal is left:
            used = flipped[used]  # 50000 < x  is  x > 50000
        for text, wanted in mentions:
            is_date = "-" in text
            if is_date:
                if not literal.is_string or not literal.name.startswith(text):
                    continue
                rest = literal.name[len(text):]
                if rest and not re.fullmatch(r"[ T]00:00(:00(\.0+)?)?\s*(Z|[-+]00(:?00)?)?", rest):
                    continue  # a time of day other than midnight changes what the bound means
            else:
                try:
                    if literal.is_string or float(literal.name) != float(text):
                        continue
                except ValueError:
                    continue
            if used == wanted or text in seen:
                continue
            seen.add(text)
            same_side = used[1] == wanted[1]
            findings.append(
                asked.finding(
                    "comparison_operator",
                    Severity.MEDIUM if same_side else Severity.HIGH,
                    f"The question's wording for {text} calls for {symbol[wanted]}, but the SQL uses "
                    f"{symbol[used]}."
                    + (" A row exactly on the boundary falls on the wrong side." if same_side else ""),
                    asked.ctx.snippet(node),
                    value=text,
                    wanted=symbol[wanted],
                    used=symbol[used],
                )
            )
    return findings


# ------------------------------------------------------------- flag columns

_FLAG_NAME = re.compile(r"^(?:is|has)_?(.+)$|^(.+?)_?(?:flag|flg|ind|yn)$", re.IGNORECASE)


def _split_words(name: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ")
    return " ".join(spaced.lower().split())


def _flags_named_but_unused(asked: _Asked) -> list[Finding]:
    """'active plans' against a table with an is_active column that the SQL never mentions."""
    findings: list[Finding] = []
    used = {c.name.lower() for c in asked.tree.find_all(exp.Column)}
    for table in asked.ctx.analysis.tables.values():
        nouns = _name_forms(table.name)
        for column in table.columns:
            match = _FLAG_NAME.match(column.name)
            if not match:
                continue
            is_boolean = T.family_of_declared(column.type, asked.ctx.dialect) == T.BOOLEAN
            if not is_boolean and not column.name.lower().startswith(("is_", "has_")):
                continue
            word = _split_words(match.group(1) or match.group(2) or "")
            if len(word) < 4 or asked.ctx.catalog.column_key(column.name).lower() in used:
                continue
            adjacent = False
            for noun in nouns:
                head, _, last = noun.rpartition(" ")
                stem = re.escape(_singular(last))
                noun_pattern = (re.escape(head) + r"\s+" if head else "") + stem + r"(?:s|es|ies)?"
                if re.search(rf"(?<![a-z]){re.escape(word)}\s+(?:[a-z-]+\s+)?{noun_pattern}(?![a-z])", asked.lower):
                    adjacent = True
                elif re.search(rf"{noun_pattern}\s+(?:that|which|who)?\s*(?:are|is|were)\s+{re.escape(word)}(?![a-z])", asked.lower):
                    adjacent = True
            if not adjacent:
                continue
            findings.append(
                asked.finding(
                    "question_flag_unused",
                    Severity.MEDIUM,
                    f"The question asks about {word} {table.name}, and {table.name} has a column "
                    f"{column.name} for that, but the SQL never looks at it.",
                    suggestion=f"Check whether a condition on {column.name} belongs in the query.",
                    column=f"{table.name}.{column.name}",
                )
            )
    return findings


# ---------------------------------------------------------- pattern matches

_PATTERN_WORDS = re.compile(
    r"\b(starts? with|starting with|begins? with|beginning with|prefix(?:ed)?|ends? with|ending with|suffix|"
    r"contains?|containing|includes? the (?:word|text|string))\b",
    re.IGNORECASE,
)


def _pattern_match(asked: _Asked) -> list[Finding]:
    match = _PATTERN_WORDS.search(asked.text)
    if match is None:
        return []
    kinds = tuple(
        getattr(exp, name)
        for name in ("Like", "ILike", "Glob", "SimilarTo", "RegexpLike", "StartsWith", "EndsWith", "Substring",
                     "Left", "Right", "StrPosition", "Contains")
        if hasattr(exp, name)
    )
    if asked.has(*kinds):
        return []
    if any(isinstance(n, exp.Anonymous) and n.name.lower() in ("instr", "strpos", "position", "starts_with", "prefix", "contains", "regexp_matches") for n in asked.tree.find_all(exp.Anonymous)):
        return []
    return [
        asked.finding(
            "pattern_match_missing",
            Severity.MEDIUM,
            f"The question asks for a partial match (\"{match.group(0)}\"), but the SQL uses no LIKE, "
            "prefix or substring test, only exact comparisons.",
            phrase=match.group(0),
        )
    ]


# ------------------------------------------------------- order and columns

_SORT_WORDS = re.compile(
    r"\b(sort(?:ed)?\s+(?:by|on|alphabetically|ascending|descending|in)|order(?:ed)?\s+by|"
    r"in\s+(?:ascending|descending|alphabetical|chronological|reverse)\s+order|sorted\b)",
    re.IGNORECASE,
)
_RETURN_LIST = re.compile(r"(?:^|[.;?)]\s+)(?:return|returns|output|show)\s+(?:only\s+|just\s+)?([^.;?]+)", re.IGNORECASE)
# "List display_name of every plan ...", "List all plans with their code and display_name"
_LIST_OF = re.compile(r"^\s*(?:list|show|give)\s+(?:the\s+)?([\w`,\s]+?)\s+(?:of|for)\s+(?:every|each|all)\b", re.IGNORECASE)
_WITH_THEIR = re.compile(r"^\s*(?:list|show|give|return)\b[^.;?]*?\bwith\s+(?:their|its)\s+([^.;?]+)", re.IGNORECASE)
_LIST_SPLIT = re.compile(r"\s*,\s*(?:and\s+)?|\s+and\s+")
_LIST_STOP = re.compile(r"\b(sorted|sort|ordered|order by|where|for|per|grouped|break|with ties|as integer|rounded)\b", re.IGNORECASE)


def _sorting(asked: _Asked) -> list[Finding]:
    match = _SORT_WORDS.search(asked.text)
    if match is None:
        return []
    root = asked.tree
    if not isinstance(root, exp.Query) or root.args.get("order") is not None:
        return []
    if asked.root is not None and asked.root.group is None and asked.root.own_aggregates():
        return []  # a single row has no order
    return [
        asked.finding(
            "order_missing",
            Severity.HIGH,
            f"The question asks for sorted output (\"{match.group(0)}\"), but the query has no ORDER BY, "
            "so the rows come back in whatever order the engine produces.",
            suggestion="Add the ORDER BY the question asks for.",
            phrase=match.group(0),
        )
    ]


def _listed_outputs(text: str, column_names: set[str] | None = None) -> int | None:
    """How many output columns a sentence such as 'Return tenant_id and the invoice count.' lists."""
    match = _RETURN_LIST.search(text) or _LIST_OF.search(text) or _WITH_THEIR.search(text)
    if match is None:
        return None
    listed = match.group(1)
    stop = _LIST_STOP.search(listed)
    if stop:
        listed = listed[: stop.start()]
    if "(" in listed or ")" in listed or " as " in f" {listed.lower()} " or "—" in listed:
        return None  # a described specification, not a plain list of names
    listed = listed.strip(" ,")
    if not listed:
        return None
    items = [item.strip() for item in _LIST_SPLIT.split(listed) if item.strip()]
    if not items or len(items) > 5 or any(len(item.split()) > 4 for item in items):
        return None  # not a plain list of columns
    if match.re is not _RETURN_LIST:
        # The looser phrasings only count when every item is the name of a real column.
        known = column_names or set()
        for item in items:
            name = re.sub(r"^(?:the|their|its)\s+", "", item.lower()).strip("`\"' ").replace(" ", "_")
            if name not in known:
                return None
    return len(items)


def _output_columns(asked: _Asked) -> list[Finding]:
    column_names = {c.name.lower() for t in asked.ctx.analysis.tables.values() for c in t.columns}
    wanted = _listed_outputs(asked.text, column_names)
    root = asked.tree
    if wanted is None or not isinstance(root, exp.Query):
        return []
    select = root
    if not isinstance(select, exp.Select) or select.is_star:
        return []  # a UNION can merge several listed things into one column
    returned = len(select.expressions)
    if returned == wanted:
        return []
    return [
        asked.finding(
            "output_columns",
            Severity.MEDIUM,
            f"The question lists {wanted} thing{'s' if wanted != 1 else ''} to return, but the query "
            f"returns {returned} column{'s' if returned != 1 else ''}.",
            wanted=wanted,
            returned=returned,
        )
    ]


# ----------------------------------------------- findings the question firms up

_NOT_DELETED = re.compile(
    r"\b(non-?deleted|not\s+(?:soft[- ])?deleted|un-?deleted|excluding\s+(?:soft[- ])?deleted|"
    r"haven't been (?:soft[- ])?deleted|have not been (?:soft[- ])?deleted|live|still exist)\b",
    re.IGNORECASE,
)
_WANTS_DELETED = re.compile(r"\b(including\s+(?:soft[- ])?deleted|deleted\s+\w+|were deleted|was deleted|soft[- ]deleted)\b", re.IGNORECASE)
_CURRENT = re.compile(r"\b(current|currently|latest|present|in force|as of now|today's)\b", re.IGNORECASE)
_EVERY = re.compile(r"\b(for every|for each|every|each|for all)\b", re.IGNORECASE)
_DISTINCT_WORDS = re.compile(r"\b(distinct|unique|different|de-?duplicated|without duplicates|no duplicates)\b", re.IGNORECASE)
_DISTINCT_ROWS = re.compile(
    r"\b(?:return|list|show|give)\s+(?:the\s+|only\s+)?(?:distinct|unique)\b|\b(?:distinct|unique)\s+(?:rows|pairs|values|list)\b|"
    r"\bwithout duplicates\b|\bno duplicates\b|\bde-?duplicated\b",
    re.IGNORECASE,
)
_ONLY_MATCHED = re.compile(
    r"\b(?:excluded|omitted|left out|not (?:listed|returned|included)|only (?:those|the ones|\w+) (?:with|that have|having)|"
    r"that have at least one|with at least one|having at least one)\b",
    re.IGNORECASE,
)
_LISTING = re.compile(r"^\s*(list|which|show|return|give|name|find|what are)\b", re.IGNORECASE)


def adjust_findings(findings: list[Finding], question: str) -> None:
    """Raise or drop earlier findings in the light of what the question says.

    A missing soft-delete filter matters more when the question says "non-deleted",
    and not at all when it asks about deleted rows; and so on.
    """
    text = " ".join((question or "").split())
    if not text:
        return
    lowered = text.lower()
    not_deleted = list(_NOT_DELETED.finditer(text))
    asks_not_deleted = bool(not_deleted)
    asks_deleted = bool(_WANTS_DELETED.search(text)) and not asks_not_deleted
    # "non-deleted tenants": which things the question wants live. Empty when it names none.
    named_live = " ".join(lowered[m.end(): m.end() + 30] for m in not_deleted)
    names_a_noun = any(re.match(r"\s*[a-z_]{3,}", lowered[m.end():]) for m in not_deleted) and not all(
        re.match(r"\s*($|[?.,;)])", lowered[m.end():]) for m in not_deleted
    )
    for finding in list(findings):
        if finding.code == "soft_deleted_rows":
            if asks_deleted:
                findings.remove(finding)
            elif asks_not_deleted and not finding.evidence.get("bridge"):
                table_name = str(finding.evidence.get("table_name") or "")
                named = _entity_position(named_live, table_name) is not None if table_name else False
                if named or (finding.evidence.get("counted") and not names_a_noun):
                    finding.severity = Severity.HIGH
                    finding.message += " The question explicitly asks for rows that are not deleted."
        elif finding.code == "history_rows" and _CURRENT.search(text):
            finding.severity = Severity.HIGH
            finding.message += " The question asks for the current state."
        elif finding.code == "duplicate_rows":
            if _DISTINCT_ROWS.search(text):
                finding.severity = Severity.HIGH
                finding.message += " The question asks for distinct rows."
            elif _LISTING.search(text) and finding.severity == Severity.LOW:
                finding.severity = Severity.MEDIUM
        elif finding.code == "filter_value" and finding.severity == Severity.HIGH:
            literal = str(finding.evidence.get("literal") or "")
            if literal and re.search(rf"(?<![a-z0-9_]){re.escape(literal.lower())}(?![a-z0-9_])", lowered):
                # The question itself names this value. That it has no rows today is a fact
                # about the data, not a sign the SQL is wrong.
                finding.severity = Severity.LOW
                finding.message += " The question names this value itself, so the filter is as asked."
        elif finding.code == "inner_join_drops_rows":
            if finding.evidence.get("limited"):
                continue  # a top-N query: things with nothing to count would rank last anyway
            if _ONLY_MATCHED.search(text):
                continue  # the question says things without a match are left out
            every = [m for m in _EVERY.finditer(text)]
            entity_table = str(finding.evidence.get("entity_table") or "")
            if not any(_entity_position(lowered[m.end(): m.end() + 40], entity_table) is not None for m in every):
                continue  # "every" is not said of the thing that would go missing
            check = finding.evidence.get("data_check")
            if check == "unmatched":
                # A count of zero is an answer the question asked for; a missing sum is more arguable.
                finding.severity = Severity.HIGH if finding.evidence.get("counts_only") else Severity.MEDIUM
                finding.message += " The question asks for every one, including those with none."
            elif check is None:
                finding.severity = Severity.MEDIUM
                finding.message += " The question asks for every one."
            else:
                finding.severity = Severity.LOW


__all__ = ["run_question_checks", "adjust_findings", "STAGE"]
