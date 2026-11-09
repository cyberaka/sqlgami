"""What the question says the answer should look like, against what the SELECT list returns.

Production questions spell the output out: ``Return (month_start, tenant_id, mrr_cents)``,
``Include these columns in this exact order: ...``, ``country_code (UNKNOWN if none)``,
``quarter labels like 'Q1 2026'``. These are checkable without running anything, and
they are where strong models most often go wrong once the data is right: a column
too many, 'Q1' for 'Q1 2026', NULL where a 0 was asked for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from .checks.base import CheckContext
from .model import SelectModel, strip, unalias
from .report import Finding, Severity

STAGE = "question"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Sentences that introduce the list of output columns.
_INTRO = re.compile(
    r"\b(?:"
    r"(?:return|returns|returning|output|outputs|include|including|show|produce|report|with|giving|list)"
    r"\s+(?:exactly\s+|only\s+|just\s+|one row per \w+ with\s+|a row with\s+)?"
    r"(?:(?:these|the following|the)\s+)?(?:\d+\s+)?columns?(?:,?\s+in (?:this|that|the following)?\s*(?:exact\s+)?order)?\s*:?\s*"
    r"|columns?\s+(?:in (?:this\s+)?(?:exact\s+)?order|\(in order\))\s*:\s*"
    r"|(?:return|returns|output|outputs|report|show|produce)\s+(?:only\s+|exactly\s+)?\("
    r"|(?:return|returns|include)\s+(?=[a-z][a-z0-9_]*(?:\s*\([^)]*\))?\s*,)"
    r"|\bwith:\s*"
    r")",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<![0-9])\.(?=\s+[A-Z(]|\s*$)|[;?]|\n\s*\n")
_STOP = re.compile(
    r"\b(sorted|sort(?:ed)? by|ordered by|order by|in that (?:exact )?order|in this (?:exact )?order|where\b|grouped by|"
    r"one row per|rows? (?:are|should)|break ties|ties broken|descending|ascending|for (?:every|each|all)|per \w+)\b",
    re.IGNORECASE,
)
_DEFAULT = re.compile(
    r"(?:\(|;|,)\s*(?P<value>NULL|0|zero|-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\"|[A-Z][A-Z_]+)\s+"
    r"(?:otherwise\b|(?:if|when|where)\s+(?:[^)]{0,40}?\b(?:no|none|null|missing|absent|empty|only|nothing)\b))",
    re.IGNORECASE,
)
_LABEL_CONTEXT = re.compile(
    r"\b(?:lab[e]l(?:s|led|ed)?|format(?:ted|s)?|like|such as|e\.g\.|for example|as|named|spelled|written|"
    r"values?|strings?)\b[^'\"]{0,40}?(['\"])(?P<text>[^'\"]{2,40})\1",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"(['\"])(?P<text>[^'\"]{2,40})\1")
_NULL_SAFE = (exp.Coalesce, exp.Nvl2, exp.Count, exp.CountIf)


@dataclass
class ListedColumn:
    text: str  # as written, trimmed
    name: str | None  # an identifier, when the item names one
    default: str | None = None  # "0", "NULL", "'UNKNOWN'" ... from "(X if none)"
    position: int = 0


@dataclass
class ColumnList:
    items: list[ListedColumn]
    ordered: bool  # the question insists on the order
    source: str  # the sentence it came from

    @property
    def names(self) -> list[str | None]:
        return [item.name for item in self.items]


def _strip_parens(text: str) -> tuple[str, list[str]]:
    """Remove (...) groups, returning what was removed; nested one level deep."""
    removed: list[str] = []
    depth = 0
    start = None
    out = []
    for i, ch in enumerate(text):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                removed.append(text[start : i + 1])
                start = None
        elif depth == 0:
            out.append(ch)
    return "".join(out), removed


def _split_top_level(text: str) -> list[str]:
    """Split on commas and ' and ' outside parentheses and quotes."""
    parts: list[str] = []
    depth = 0
    quote = None
    current = []
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch == '"' or (ch == "'" and not (i and text[i - 1].isalnum())):
            quote = ch
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        elif depth == 0 and text[i : i + 5].lower() == " and " and not current[-1:] == ["="]:
            parts.append("".join(current))
            current = []
            i += 4
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return [p.strip(" ,;.") for p in parts if p.strip(" ,;.")]


def _item(text: str, position: int) -> ListedColumn | None:
    raw = text.strip()
    article = re.match(r"^(?:and\s+)?(?:the|their|its|a|an)\s+", raw, flags=re.IGNORECASE) is not None
    raw = re.sub(r"^(?:and\s+)?(?:the|their|its|a|an)\s+", "", raw, flags=re.IGNORECASE)
    bare, parens = _strip_parens(raw)
    bare = bare.strip(" ,")
    default = None
    for group in parens:
        found = _DEFAULT.search(group)
        if found:
            default = found.group("value")
            break
    # "rk = the transition's rank ...", "tax_cents = FLOOR(...)": the name is what precedes "=".
    head = bare.split("=")[0].strip()
    head = head.strip("`\"'")
    words = head.split()
    name = None
    aliased = re.match(r"^(\S+)\s+as\s+[`\"']?([A-Za-z_][A-Za-z0-9_]*)[`\"']?$", head, flags=re.IGNORECASE)
    if aliased:
        name = aliased.group(2)  # "the row_number as `seq`": the output column is seq
    elif words and _IDENT.match(words[0]) and (len(words) == 1 or words[1].lower() in ("as", "=")):
        name = words[0]
        if article and len(words) == 1 and "_" not in name and not re.search(r"\d", name):
            name = None  # "the count", "the name": a description, not necessarily the column's name
    elif words and _IDENT.match(words[-1]) and len(words) <= 3 and "_" in words[-1]:
        name = words[-1]  # "the pct_e6 used" is not this; "two flags only_inv" is caught below
    if name is None and "/" in head and all(_IDENT.match(w) for w in head.split("/")):
        return None  # "only_inv/only_pay": two names in one item, handled by the caller
    if not bare and not parens:
        return None
    return ListedColumn(raw, name.lower() if name else None, default, position)


def parse_column_list(question: str) -> ColumnList | None:
    """The output columns a question spells out, or ``None`` when it does not."""
    text = " ".join(question.split())
    for intro in _INTRO.finditer(text):
        start = intro.end()
        tail = text[start - 1 :] if text[start - 1] == "(" else text[start:]
        ordered = bool(re.search(r"\border\b", intro.group(0), re.IGNORECASE))
        if tail.startswith("("):
            depth = 0
            end = None
            for i, ch in enumerate(tail):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end is None:
                continue
            body = tail[1:end]
            ordered = True
        else:
            body = _sentence(tail)
            cut = _STOP.search(body)
            if cut:
                if re.search(r"in (?:this|that) (?:exact )?order", cut.group(0), re.IGNORECASE):
                    ordered = True
                body = body[: cut.start()]
        parts = _split_top_level(body)
        items: list[ListedColumn] = []
        bad = False
        for part in parts:
            bare, _ = _strip_parens(part)
            bare = re.sub(r"^(?:and\s+)?(?:the\s+)?(?:two|three|four|five|\d+)\s+\w+\s+", "", bare.strip(), flags=re.IGNORECASE)
            slashed = re.match(r"^([A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)+)\b", bare)
            if slashed:
                for sub in slashed.group(1).split("/"):
                    items.append(ListedColumn(sub, sub.lower(), None, len(items)))
                continue
            item = _item(part, len(items))
            if item is None:
                bad = True
                break
            if item.name is None and len(item.text.split()) > 12:
                bad = True  # a paragraph, not a column
                break
            items.append(item)
        if bad or len(items) < 2:
            continue
        if sum(1 for i in items if i.name) < max(2, len(items) // 2):
            continue  # mostly prose: too loose to hold the query to
        return ColumnList(items, ordered, body.strip())
    return None


def _sentence(text: str) -> str:
    """Up to the end of the sentence, not counting punctuation inside parentheses or quotes."""
    depth = 0
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch == '"' or (ch == "'" and not (i and text[i - 1].isalnum())):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            if ch in ";?":
                return text[:i]
            if ch == "." and (i + 1 == len(text) or (text[i + 1] == " " and (i + 2 >= len(text) or text[i + 2].isupper() or text[i + 2] == "("))):
                if i and text[i - 1].isdigit() and i + 2 < len(text) and text[i + 2].isdigit():
                    continue
                return text[:i]
    return text


def _output_names(select: exp.Select) -> list[str]:
    return [(p.alias_or_name or "").lower() for p in select.expressions]


def _counting_subquery(node: exp.Expression) -> bool:
    """``(SELECT COUNT(*) FROM ...)`` with no GROUP BY: always one row, never NULL."""
    inner = node.this if isinstance(node, exp.Subquery) else None
    if not isinstance(inner, exp.Select) or inner.args.get("group") is not None or len(inner.expressions) != 1:
        return False
    return isinstance(strip(unalias(inner.expressions[0])), (exp.Count, exp.CountIf))


def _null_safe(expression: exp.Expression, model: SelectModel | None) -> bool:
    node = strip(unalias(expression))
    if isinstance(node, exp.Literal):
        return True
    if isinstance(node, exp.Subquery) and _counting_subquery(node):
        return True
    if isinstance(node, _NULL_SAFE):
        return True
    if isinstance(node, exp.Anonymous) and node.name.lower() in ("ifnull", "nvl", "isnull", "zeroifnull", "if", "iif"):
        return True
    if isinstance(node, exp.If):
        return True
    if isinstance(node, exp.Case):
        default = node.args.get("default")
        if default is not None and not (isinstance(default, exp.Null)):
            conditions = [strip(c.this) for c in node.args.get("ifs") or []]
            if any(isinstance(c, exp.Is) for c in conditions) or default is not None:
                return True
    if isinstance(node, exp.Cast):
        return _null_safe(node.this, model)
    for inner in node.walk():
        if isinstance(inner, (exp.Coalesce, exp.Nvl2)) and inner is not node:
            return True  # COALESCE applied somewhere inside the expression
    # A column that comes from a derived table where it was already coalesced.
    if isinstance(node, exp.Column) and model is not None:
        try:
            inner_model, defining = model.origin(node)
        except Exception:
            return False
        if defining is not None and inner_model is not None and inner_model is not model:
            return _null_safe(defining, inner_model)
    return False


def _coalesces_to_value(expression: exp.Expression) -> bool:
    """COALESCE(x, 0) and the like; COALESCE(x, NULL) substitutes nothing."""
    node = strip(unalias(expression))
    if isinstance(node, exp.Coalesce):
        return any(not isinstance(strip(e), exp.Null) for e in node.expressions)
    if isinstance(node, exp.Anonymous) and node.name.lower() in ("ifnull", "nvl", "zeroifnull"):
        return any(not isinstance(strip(e), exp.Null) for e in node.expressions[1:]) or node.name.lower() == "zeroifnull"
    return isinstance(node, exp.Nvl2)


def check_shape(ctx: CheckContext, question: str, root_model: SelectModel | None) -> list[Finding]:
    """output_columns, column_order, column_default and label_format findings.

    Each rests on something the question states outright, so none of them is softened on
    long questions the way word-matching heuristics are.
    """
    findings: list[Finding] = []
    tree = ctx.analysis.resolved
    if tree is None or not isinstance(tree, exp.Select) or tree.is_star:
        return findings
    listed = parse_column_list(question)
    names = _output_names(tree)
    if listed is not None:
        findings.extend(_column_findings(listed, tree, names, root_model))
    findings.extend(_label_findings(question, tree))
    for finding in findings:
        finding.evidence["explicit"] = True
    return findings


def _column_findings(listed: ColumnList, select: exp.Select, names: list[str], model: SelectModel | None) -> list[Finding]:
    findings: list[Finding] = []
    wanted = len(listed.items)
    returned = len(names)
    named = [i.name for i in listed.items if i.name]
    missing = [n for n in named if n not in names]
    fully_named = len(named) == len(listed.items)
    if not fully_named:
        pass  # "invoice counts for each month" may be two columns: the count cannot be held to
    elif returned != wanted:
        detail = ""
        if missing:
            detail = f" The question's {', '.join(missing[:4])} {'is' if len(missing) == 1 else 'are'} not among them."
        findings.append(
            Finding(
                "output_columns",
                Severity.MEDIUM,
                f"The question lists {wanted} columns to return ({_short(listed.source)}), but the query "
                f"returns {returned}.{detail}",
                STAGE,
                suggestion="Return exactly the columns the question lists, in its order.",
                evidence={"wanted": wanted, "returned": returned, "missing": missing, "listed": listed.names},
            )
        )
    elif missing and len(missing) <= len(named) // 2 + 1:
        # Same count; some named columns are not there under that name. Aliases differ often
        # enough (n_invoices vs invoice_count) that this is a note unless most names are off.
        findings.append(
            Finding(
                "output_columns",
                Severity.LOW if len(missing) < len(named) else Severity.MEDIUM,
                f"The question names {', '.join(named)} as the output columns; the query's columns are named "
                f"{', '.join(n for n in names if n)}. {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} "
                "missing under that name.",
                STAGE,
                evidence={"missing": missing, "listed": listed.names, "returned_names": names},
            )
        )
    present = [n for n in named if n in names]
    if not (fully_named and returned != wanted) and len(present) >= 2:
        positions = [names.index(n) for n in present]
        if positions != sorted(positions):
            findings.append(
                Finding(
                    "column_order",
                    Severity.MEDIUM if listed.ordered else Severity.LOW,
                    f"The question lists the columns {'in this exact order' if listed.ordered else 'in an order'} "
                    f"({', '.join(named)}); the query returns them in another ({', '.join(n for n in names if n)}).",
                    STAGE,
                    suggestion="Match the question's column order.",
                    evidence={"listed": named, "returned_names": names, "insisted": listed.ordered},
                )
            )
    # Defaults the question spells out for a column: "(0 if none)", "(NULL when ...)", "(UNKNOWN if none)".
    for item in listed.items:
        if item.default is None or item.name is None or item.name not in names:
            continue
        expression = select.expressions[names.index(item.name)]
        wants_null = item.default.upper() == "NULL"
        if wants_null:
            if _coalesces_to_value(expression):
                findings.append(
                    Finding(
                        "column_default",
                        Severity.LOW,
                        f"The question says {item.name} should be NULL when there is nothing to report, but the "
                        f"query substitutes a value ({_short(expression.sql(dialect=select.sql_name if False else None)[:80])}).",
                        STAGE,
                        evidence={"column": item.name, "wanted": "NULL"},
                    )
                )
            continue
        if not _null_safe(expression, model) and _can_be_null(expression, model):
            findings.append(
                Finding(
                    "column_default",
                    Severity.MEDIUM,
                    f"The question says {item.name} should be {item.default} when there is nothing to report, "
                    f"but the query's {item.name} is {_short(expression.sql()[:100])}, which is NULL in that case.",
                    STAGE,
                    suggestion=f"Wrap it in COALESCE(..., {item.default}).",
                    evidence={"column": item.name, "wanted": item.default},
                )
            )
    return findings


def _can_be_null(expression: exp.Expression, model: SelectModel | None) -> bool:
    """Whether the expression could come out NULL: an outer-joined column, a scalar subquery, SUM/MIN/MAX..."""
    node = strip(unalias(expression))
    for inner in node.walk():
        if isinstance(inner, exp.Subquery) and not _counting_subquery(inner):
            return True
        if isinstance(inner, exp.AggFunc) and not isinstance(inner, (exp.Count, exp.CountIf)):
            return True
        if isinstance(inner, exp.Column) and model is not None:
            source = model.sources.get(inner.table)
            if source is not None and getattr(source, "null_supplied", False):
                return True
    if isinstance(node, exp.Column) and model is not None:
        source = model.sources.get(node.table)
        if source is not None and source.kind == "derived":
            return True  # cannot see inside well enough to rule it out
    return False


def _label_findings(question: str, select: exp.Select) -> list[Finding]:
    """A label the question spells out ('Q1 2026') must be producible by the query's string literals."""
    findings: list[Finding] = []
    labels: list[str] = []
    for match in _LABEL_CONTEXT.finditer(question):
        text = match.group("text")
        if re.search(r"[A-Za-z]", text) and re.search(r"\d", text) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
            labels.append(text)
    # "(Q1 = Jan-Mar, Q2 = Apr-Jun)": the labels are defined bare, and the query must not embellish them.
    defined: list[str] = []
    for group in re.finditer(r"\(([^()]{3,120})\)", question):
        pairs = re.findall(r"(?:^|,)\s*([A-Z][A-Za-z0-9]{0,5})\s*=\s*[^,]+", group.group(1))
        if len(pairs) >= 2 and all(re.search(r"\d", p) for p in pairs):
            defined.extend(pairs)
    if defined:
        texts = _sql_strings(select)
        longer = sorted({t for t in texts for d in defined if t != d and t.startswith(d) and len(t) > len(d)})
        if longer and not any(t in defined for t in texts):
            findings.append(
                Finding(
                    "label_format",
                    Severity.MEDIUM,
                    f"The question defines the labels {', '.join(repr(d) for d in defined)}; the query writes "
                    f"{', '.join(repr(t) for t in longer[:4])} instead.",
                    STAGE,
                    suggestion="Use the labels exactly as the question defines them.",
                    evidence={"defined": defined, "written": longer[:8]},
                )
            )
    if not labels:
        return findings
    skeletons = _sql_string_skeletons(select)
    for label in dict.fromkeys(labels):
        wanted = _skeleton(label)
        if any(s == wanted for s in skeletons):
            continue
        findings.append(
            Finding(
                "label_format",
                Severity.MEDIUM,
                f"The question wants labels written like '{label}', but no string the query builds has that "
                f"shape (found: {', '.join(repr(s) for s in sorted(set(skeletons))[:6]) or 'none'}).",
                STAGE,
                suggestion=f"Build the label exactly as '{label}'.",
                evidence={"label": label, "skeletons": sorted(set(skeletons))[:12]},
            )
        )
    return findings


def _sql_strings(select: exp.Select) -> list[str]:
    """Every string the query writes out, concatenations joined with '#' for the computed parts."""
    out: list[str] = []
    seen: set[int] = set()
    for node in select.walk():
        if id(node) in seen or not isinstance(node, (exp.DPipe, exp.Concat, exp.Literal)):
            continue
        text = _pieces(node)
        if text is not None:
            out.append(text)
            for inner in node.walk():
                seen.add(id(inner))
    return out


def _pieces(node: exp.Expression) -> str | None:
    node = strip(node)
    if isinstance(node, exp.Literal) and node.is_string:
        return node.name
    if isinstance(node, exp.DPipe):
        left, right = _pieces(node.this), _pieces(node.expression)
        return (left if left is not None else "#") + (right if right is not None else "#")
    if isinstance(node, exp.Concat):
        return "".join(p if (p := _pieces(e)) is not None else "#" for e in node.expressions)
    return None


def _skeleton(text: str) -> str:
    text = re.sub(r"%[-0-9.]*[a-zA-Z]", "#", text)  # printf-style
    text = re.sub(r"\d+", "#", text)
    return re.sub(r"#+", "#", text).strip()


def _sql_string_skeletons(select: exp.Select) -> list[str]:
    """Shapes of the strings the query can produce: literals, concatenations, FORMAT patterns."""
    found: list[str] = []
    seen: set[int] = set()

    def pieces(node: exp.Expression) -> str | None:
        node = strip(node)
        if isinstance(node, exp.Literal) and node.is_string:
            return node.name
        if isinstance(node, exp.DPipe):
            left, right = pieces(node.this), pieces(node.expression)
            return (left if left is not None else "#") + (right if right is not None else "#")
        if isinstance(node, exp.Concat):
            return "".join(p if (p := pieces(e)) is not None else "#" for e in node.expressions)
        if isinstance(node, exp.Anonymous) and node.name.lower() in ("format", "printf", "strftime", "to_char"):
            first = next((e for e in node.expressions if isinstance(strip(e), exp.Literal) and strip(e).is_string), None)
            return strip(first).name if first is not None else None
        return None

    for node in select.walk():
        if id(node) in seen:
            continue
        if isinstance(node, (exp.DPipe, exp.Concat, exp.Anonymous, exp.Literal)):
            text = pieces(node)
            if text is not None:
                found.append(_skeleton(text))
                for inner in node.walk():
                    seen.add(id(inner))
    return found


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
