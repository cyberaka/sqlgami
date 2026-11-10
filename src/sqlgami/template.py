"""SQL that arrives as a template: ``{name}`` placeholders, comments, disabled filters.

Applications often keep SQL as a Python format string and fill it in just
before running it. Such text is not valid SQL as written, and a parser may
swallow the braces without complaint, so placeholders are found here, before
parsing, and replaced with query parameters for analysis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from .report import Finding, Severity

CODE = "code"
STRING = "string"
COMMENT = "comment"
QUOTED_NAME = "name"

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DISABLED = re.compile(
    r"^\s*(?:(?:AND|OR|WHERE|HAVING|ON)\b.+|[\w.`\"]+\s*(?:=|<>|!=|<=|>=|<|>|\bIN\b|\bNOT\s+IN\b|\bLIKE\b|\bIS\b|\bBETWEEN\b).+)$",
    re.IGNORECASE | re.DOTALL,
)
_DECLARE_DEFAULT = re.compile(
    r"\bDECLARE\s+[\w,\s]+?\s+([A-Za-z][A-Za-z0-9_]*)(?:\s*\([^)]*\))?\s+DEFAULT\s*$", re.IGNORECASE
)
_IN_BEFORE = re.compile(r"\bIN\s*\(\s*$", re.IGNORECASE)
_CLOSE_AFTER = re.compile(r"^\s*\)")

# Dialects where a backslash escapes the next character inside a string.
_BACKSLASH_ESCAPES = {"bigquery", "mysql", "spark", "databricks", "hive", "clickhouse"}
# Dialects where "..." is a string rather than a quoted name.
_DOUBLE_QUOTED_STRINGS = {"bigquery", "mysql", "spark", "databricks", "hive"}
_HASH_COMMENTS = {"bigquery", "mysql"}


@dataclass
class TemplateParam:
    name: str
    quoted: bool = False  # written inside quotes, so the value is spliced into a string
    inferred_type: str | None = None
    occurrences: int = 0
    supplied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "quoted": self.quoted,
            "inferred_type": self.inferred_type,
            "occurrences": self.occurrences,
            "supplied": self.supplied,
        }


@dataclass
class Prepared:
    original: str
    analysis_sql: str  # placeholders replaced by query parameters; safe to parse
    executable_sql: str | None  # what could be sent to the database, or None if values are missing
    params: list[TemplateParam] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    disabled_filters: list[str] = field(default_factory=list)

    @property
    def is_template(self) -> bool:
        return bool(self.params)


def segments(sql: str, dialect: str) -> Iterator[tuple[str, str]]:
    """Split SQL text into code, string, quoted-name and comment pieces."""
    backslash = dialect in _BACKSLASH_ESCAPES
    double_is_string = dialect in _DOUBLE_QUOTED_STRINGS
    hash_comments = dialect in _HASH_COMMENTS
    i, n = 0, len(sql)
    start = 0

    def flush(end: int) -> Iterator[tuple[str, str]]:
        if end > start:
            yield CODE, sql[start:end]

    while i < n:
        ch = sql[i]
        if (ch == "-" and sql.startswith("--", i)) or (ch == "#" and hash_comments):
            yield from flush(i)
            end = sql.find("\n", i)
            end = n if end == -1 else end
            yield COMMENT, sql[i:end]
            i = start = end
            continue
        if ch == "/" and sql.startswith("/*", i):
            yield from flush(i)
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            yield COMMENT, sql[i:end]
            i = start = end
            continue
        if ch in ("'", '"', "`"):
            yield from flush(i)
            end = i + 1
            while end < n:
                if backslash and sql[end] == "\\" and ch != "`":
                    end += 2
                    continue
                if sql[end] == ch:
                    if end + 1 < n and sql[end + 1] == ch and ch != "`":
                        end += 2
                        continue
                    break
                end += 1
            end = min(end + 1, n)
            is_string = ch == "'" or (ch == '"' and double_is_string)
            yield (STRING if is_string else QUOTED_NAME), sql[i:end]
            i = start = end
            continue
        i += 1
    yield from flush(n)


def _render(value: Any) -> str:
    """A value as ``str.format`` would write it, except that a list becomes a comma-separated
    list of SQL literals (``IN ({codes})`` with ``["A", "B"]`` gives ``IN ('A', 'B')``)."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(_literal(v) for v in value)
    return str(value)


def literal_node(value: Any) -> Any:
    """``value`` as a sqlglot literal expression (a list becomes a tuple of literals)."""
    from sqlglot import exp

    if isinstance(value, (list, tuple, set, frozenset)):
        return exp.Tuple(expressions=[literal_node(v) for v in value])
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _marker(name: str, dialect: str) -> str:
    """A query parameter the dialect's parser accepts anywhere a value can stand (``SELECT {p} AS x``
    as well as ``WHERE c = {p}``)."""
    if dialect == "bigquery":
        return f"@{name}"
    if dialect == "duckdb":
        return f"${name}"  # DuckDB's parser rejects :name as a projection
    return f":{name}"


def _comment_body(text: str) -> str:
    if text.startswith("--"):
        return text[2:].strip()
    if text.startswith("#"):
        return text[1:].strip()
    return text[2:-2].strip() if text.endswith("*/") else text[2:].strip()


def prepare(sql: str, dialect: str, values: dict[str, Any] | None = None) -> Prepared:
    """Find placeholders and disabled filters; build the text to analyse and the text to run."""
    values = dict(values or {})
    parts = list(segments(sql, dialect))
    params: dict[str, TemplateParam] = {}
    findings: list[Finding] = []
    disabled: list[str] = []
    analysis: list[str] = []
    rendered: list[str] = []
    missing = False

    def note(name: str, quoted: bool, inferred: str | None) -> TemplateParam:
        param = params.setdefault(name, TemplateParam(name))
        param.occurrences += 1
        param.quoted = param.quoted or quoted
        param.inferred_type = param.inferred_type or inferred
        param.supplied = name in values
        return param

    for index, (kind, text) in enumerate(parts):
        if kind == COMMENT:
            body = _comment_body(text)
            for line in body.splitlines() or [body]:
                line = line.strip().lstrip("-").strip()
                if line and _DISABLED.match(line) and not _PLACEHOLDER.fullmatch(line):
                    disabled.append(" ".join(line.split()))
            analysis.append(text)
            rendered.append(text)
            continue

        if kind == STRING:
            inner = text[1:-1]
            whole = _PLACEHOLDER.fullmatch(inner)
            if whole:
                name = whole.group(1) or whole.group(2)
                note(name, True, "STRING")
                before = "".join(t for _, t in parts[:index])
                after = "".join(t for _, t in parts[index + 1 :])
                in_list = bool(_IN_BEFORE.search(before)) and bool(_CLOSE_AFTER.match(after))
                value = values.get(name)
                if in_list:
                    as_text = "" if value is None else str(value)
                    confirmed = name in values and "," in as_text
                    findings.append(
                        Finding(
                            "template_list_param",
                            Severity.HIGH if confirmed else Severity.MEDIUM,
                            f"IN ('{{{name}}}') puts one quoted placeholder inside the list. "
                            + (
                                f"The value supplied, {as_text!r}, contains commas, so the database "
                                "sees a single string and the condition matches nothing."
                                if confirmed
                                else "If it is filled with several comma-separated values, the "
                                "database sees a single string and the condition matches nothing."
                            ),
                            "template",
                            snippet=f"IN ({text})",
                            suggestion="Pass the list as an array parameter (IN UNNEST(@param)), or build "
                            "one quoted element per value.",
                            evidence={"parameter": name},
                        )
                    )
                analysis.append(_marker(name, dialect))
                if name in values:
                    rendered.append(text[0] + _PLACEHOLDER.sub(str(values[name]), inner) + text[-1])
                else:
                    missing = True
                    rendered.append(text)
                continue
            if _PLACEHOLDER.search(inner):

                def embedded(match: re.Match) -> str:
                    nonlocal missing
                    name = match.group(1) or match.group(2)
                    note(name, True, "STRING")
                    if name not in values:
                        missing = True
                    return f"__param_{name}__"

                analysis.append(text[0] + _PLACEHOLDER.sub(embedded, inner) + text[-1])
                rendered.append(
                    text[0]
                    + _PLACEHOLDER.sub(
                        lambda m: str(values.get(m.group(1) or m.group(2), m.group(0))), inner
                    )
                    + text[-1]
                )
                continue
            analysis.append(text)
            rendered.append(text)
            continue

        if kind == QUOTED_NAME or not _PLACEHOLDER.search(text):
            analysis.append(text)
            rendered.append(text)
            continue

        out_analysis: list[str] = []
        out_rendered: list[str] = []
        last = 0
        for match in _PLACEHOLDER.finditer(text):
            name = match.group(1) or match.group(2)
            prefix = text[last : match.start()]
            declared = _DECLARE_DEFAULT.search("".join(analysis) + "".join(out_analysis) + prefix)
            note(name, False, declared.group(1).upper() if declared else None)
            out_analysis.append(prefix + _marker(name, dialect))
            if name in values:
                out_rendered.append(prefix + _render(values[name]))
            else:
                missing = True
                out_rendered.append(prefix + match.group(0))
            last = match.end()
        out_analysis.append(text[last:])
        out_rendered.append(text[last:])
        analysis.append("".join(out_analysis))
        rendered.append("".join(out_rendered))

    for param in params.values():
        param.supplied = param.name in values
    return Prepared(
        original=sql,
        analysis_sql="".join(analysis),
        executable_sql=None if missing else "".join(rendered),
        params=list(params.values()),
        findings=findings,
        disabled_filters=disabled,
    )
