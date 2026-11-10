"""The term catalogue: how a host's business terms and KPIs are represented in its data.

Authored as a spreadsheet — one ``.xlsx`` with the sheets below, or the same sheets as CSV files
in a folder (``Terms.csv``, ``KPIs.csv``, ``Examples.csv``, ``Parameters.csv``), which diff well
in git. YAML and the Python API carry the same rows for advanced use. Nothing here is built
in: sqlgami ships no vocabulary. A term the catalogue does not define cannot be confirmed.

Sheets, one row each, plain text columns:

* **Terms**: ``term | phrases | kind | table | sql | nulls | description``. ``phrases`` are
  separated by ``;`` and may hold ``{n}`` for a number the question gives and ``{param}`` for a
  template parameter. ``kind`` is ``filter`` (``sql`` is a condition rows must satisfy),
  ``formula`` (``sql`` is how a value is computed; ``{x}`` names its input), ``window``
  (``sql`` is a range on a column: ``period_seq BETWEEN -({n}-1) AND 0``, ``period_seq = -{n}``
  or ``ROWS BETWEEN {n}-1 PRECEDING AND CURRENT ROW``), ``source`` (``sql`` is the
  ``table.column`` that holds the value), ``chain`` (``sql`` is ``col1, col2, ... = {param}``,
  an access chain) or ``rank`` (``sql`` says how a "top {n}" is cut: ``LIMIT {n}``,
  ``DENSE_RANK() <= {n}``, ``RANK() <= {n}`` or ``ROW_NUMBER() <= {n}``). A row with a blank
  ``sql`` is documentation only: it is matched, so the
  term is not reported as undefined, but nothing is enforced and the score is capped as for an
  undefined term.
* **KPIs**: ``kpi | phrases | formula | format | requires | reference_sql | description``.
  ``formula`` is over terms (``{term_name}``) or plain SQL; ``format`` such as ``integer`` or
  ``integer bp`` says what the output must look like; ``requires`` lists (``;``-separated)
  the terms whose conditions the KPI needs — each is checked as if the question had asked for
  it; ``reference_sql`` is a verified query for the compile step (ADR 0002), not used live.
* **Examples**: ``id | question | params | correct_sql | expected_result | dataset`` — training
  data for the compile step; validated here, not used live.
* **Parameters**: ``name | type | example_values``.

Everything is kept as text: ids keep their leading zeros, nothing is read as a date.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import sqlglot
from sqlglot import exp

from .errors import ConfigurationError

KINDS = ("filter", "formula", "window", "source", "chain", "rank")
RANK_METHODS = ("limit", "rank", "dense_rank", "row_number")
NULLS = ("", "include", "exclude", "separate")
TERM_COLUMNS = ("term", "phrases", "kind", "table", "sql", "nulls", "description", "qualifiers")
KPI_COLUMNS = ("kpi", "phrases", "formula", "format", "requires", "reference_sql", "description")
EXAMPLE_COLUMNS = ("id", "question", "params", "correct_sql", "expected_result", "dataset")
PARAMETER_COLUMNS = ("name", "type", "example_values")
SHEETS = {"terms": TERM_COLUMNS, "kpis": KPI_COLUMNS, "examples": EXAMPLE_COLUMNS, "parameters": PARAMETER_COLUMNS}


@dataclass
class Problem:
    sheet: str
    row: int  # 1-based data row (the header is row 0), 0 when the problem is about the sheet
    column: str
    message: str

    def __str__(self) -> str:
        where = f"{self.sheet} row {self.row}" if self.row else self.sheet
        return f"{where}: {self.column + ': ' if self.column else ''}{self.message}"


@dataclass
class Term:
    name: str
    phrases: tuple[str, ...] = ()
    kind: str = ""  # one of KINDS, or "" for a documentation-only row
    table: str = ""
    sql: str = ""
    nulls: str = ""
    description: str = ""
    dialects: dict[str, str] = field(default_factory=dict)  # dialect -> sql override
    row: int = 0
    qualifiers: tuple[str, ...] = ()  # words a question may put inside a phrase ("trailing {n} biweekly pay periods")

    @property
    def enforceable(self) -> bool:
        return bool(self.sql.strip()) and self.kind in KINDS

    def sql_for(self, dialect: str) -> str:
        return self.dialects.get(dialect) or self.sql


@dataclass
class KPI:
    name: str
    phrases: tuple[str, ...] = ()
    formula: str = ""
    format: str = ""
    requires: tuple[str, ...] = ()  # terms whose conditions the KPI needs
    reference_sql: str = ""
    description: str = ""
    row: int = 0

    @property
    def enforceable(self) -> bool:
        return bool(self.formula.strip() or self.format.strip() or self.requires)


@dataclass
class Example:
    id: str
    question: str
    params: str = ""
    correct_sql: str = ""
    expected_result: str = ""
    dataset: str = ""
    row: int = 0


@dataclass
class Parameter:
    name: str
    type: str = ""
    example_values: str = ""
    row: int = 0


@dataclass
class Match:
    """A phrase of a term or KPI found in the question."""

    name: str
    phrase: str
    span: tuple[int, int]
    bindings: dict[str, str]  # {n: "26"} from the phrase's placeholders
    kind: str  # the term's kind, "kpi", or "" for documentation-only

    def to_dict(self) -> dict[str, Any]:
        return {"term": self.name, "phrase": self.phrase, "span": list(self.span), "bindings": self.bindings, "kind": self.kind}


class Catalogue:
    """Terms, KPIs, examples and parameters, with validation and question matching."""

    def __init__(
        self,
        terms: Sequence[Term] = (),
        kpis: Sequence[KPI] = (),
        examples: Sequence[Example] = (),
        parameters: Sequence[Parameter] = (),
        source: str = "",
    ) -> None:
        self.terms = list(terms)
        self.kpis = list(kpis)
        self.examples = list(examples)
        self.parameters = list(parameters)
        self.source = source
        self._patterns: list[tuple[re.Pattern[str], str, str, str]] | None = None

    # ----------------------------------------------------------------- loading

    @classmethod
    def load(cls, source: Any) -> "Catalogue":
        """From a folder of CSV sheets, an ``.xlsx`` workbook, a YAML/CSV text, a dict, a list of
        term rows, or a Catalogue."""
        if isinstance(source, Catalogue):
            return source
        if isinstance(source, (str, Path)) and _looks_like_path(source):
            path = Path(source)
            if path.is_dir():
                return cls.from_csv_folder(path)
            if path.suffix.lower() in (".xlsx", ".xlsm"):
                return cls.from_xlsx(path)
            if path.suffix.lower() == ".csv":
                return cls.from_sheets({"terms": path.read_text(encoding="utf-8-sig")}, source=str(path))
            return cls.from_yaml(path.read_text(encoding="utf-8"), source=str(path))
        if isinstance(source, str):
            return cls.from_yaml(source)
        if isinstance(source, dict):
            return cls.from_dict(source)
        if isinstance(source, (list, tuple)):
            return cls.from_dict({"terms": list(source)})
        raise ConfigurationError("terms must be a folder of CSV sheets, an .xlsx file, YAML text, a dict or a list of rows")

    @classmethod
    def from_csv_folder(cls, folder: Path) -> "Catalogue":
        sheets: dict[str, str] = {}
        for file in sorted(folder.iterdir()):
            if file.suffix.lower() != ".csv":
                continue
            key = file.stem.lower().replace(" ", "")
            if key in SHEETS:
                sheets[key] = file.read_text(encoding="utf-8-sig")
        if "terms" not in sheets:
            raise ConfigurationError(f"{folder} has no Terms.csv")
        return cls.from_sheets(sheets, source=str(folder))

    @classmethod
    def from_xlsx(cls, path: Path) -> "Catalogue":
        try:
            import openpyxl  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ConfigurationError("reading an .xlsx catalogue needs openpyxl: pip install sqlgami[excel]") from exc
        book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        sheets: dict[str, list[list[str]]] = {}
        for sheet in book.worksheets:
            key = sheet.title.lower().replace(" ", "")
            if key not in SHEETS:
                continue
            rows: list[list[str]] = []
            for row in sheet.iter_rows(values_only=True):
                rows.append([_cell_text(v) for v in row])
            sheets[key] = rows
        book.close()
        if "terms" not in sheets:
            raise ConfigurationError(f"{path} has no Terms sheet")
        return cls.from_rows(sheets, source=str(path))

    @classmethod
    def from_sheets(cls, sheets: dict[str, str], source: str = "") -> "Catalogue":
        """From CSV texts keyed by sheet name."""
        rows = {key: [list(r) for r in csv.reader(io.StringIO(text))] for key, text in sheets.items()}
        return cls.from_rows(rows, source=source)

    @classmethod
    def from_rows(cls, sheets: dict[str, list[list[str]]], source: str = "") -> "Catalogue":
        """From rows of text (the first row of each sheet is its header)."""
        records = {key: _records(rows, SHEETS[key]) for key, rows in sheets.items() if key in SHEETS}
        return cls.from_dict({key: value for key, value in records.items()}, source=source)

    @classmethod
    def from_yaml(cls, text: str, source: str = "") -> "Catalogue":
        import yaml

        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"the term catalogue is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigurationError("the term catalogue must be a mapping with a 'terms' list")
        return cls.from_dict(data, source=source)

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str = "") -> "Catalogue":
        terms = [_term(row, i + 1) for i, row in enumerate(data.get("terms") or [])]
        kpis = [_kpi(row, i + 1) for i, row in enumerate(data.get("kpis") or [])]
        examples = [_example(row, i + 1) for i, row in enumerate(data.get("examples") or [])]
        parameters = [_parameter(row, i + 1) for i, row in enumerate(data.get("parameters") or [])]
        return cls(terms, kpis, examples, parameters, source=source)

    # -------------------------------------------------------------- validation

    def validate(self, dialect: str = "duckdb") -> list[Problem]:
        """Problems by sheet, row and column. An empty list means the catalogue is usable."""
        problems: list[Problem] = []
        names: dict[str, int] = {}
        for term in self.terms:
            if not term.name:
                problems.append(Problem("Terms", term.row, "term", "is blank"))
                continue
            if term.name.lower() in names:
                problems.append(Problem("Terms", term.row, "term", f"'{term.name}' is also on row {names[term.name.lower()]}"))
            names[term.name.lower()] = term.row
            if not term.phrases:
                problems.append(Problem("Terms", term.row, "phrases", "is blank; the term can never be matched"))
            if term.kind and term.kind not in KINDS:
                problems.append(Problem("Terms", term.row, "kind", f"'{term.kind}' is not one of {', '.join(KINDS)}"))
            for qualifier in term.qualifiers:
                if len(normalise(qualifier).split()) != 1:
                    problems.append(Problem("Terms", term.row, "qualifiers", f"'{qualifier}' is not a single word"))
            if term.nulls not in NULLS:
                problems.append(Problem("Terms", term.row, "nulls", f"'{term.nulls}' is not include, exclude or separate"))
            if term.sql.strip() and not term.kind:
                problems.append(Problem("Terms", term.row, "kind", "is blank although sql is given"))
            if term.enforceable:
                problems.extend(Problem("Terms", term.row, "sql", m) for m in _check_term_sql(term, dialect))
            for phrase in term.phrases:
                for marker in re.findall(r"\{(\w+)\}", phrase):
                    if marker not in ("n", "m") and not any(p.name == marker for p in self.parameters):
                        problems.append(Problem("Terms", term.row, "phrases", f"placeholder {{{marker}}} is not a Parameters row"))
        kpi_names: dict[str, int] = {}
        for kpi in self.kpis:
            if not kpi.name:
                problems.append(Problem("KPIs", kpi.row, "kpi", "is blank"))
                continue
            if kpi.name.lower() in kpi_names or kpi.name.lower() in names:
                problems.append(Problem("KPIs", kpi.row, "kpi", f"'{kpi.name}' is also a term or another KPI"))
            kpi_names[kpi.name.lower()] = kpi.row
            if not kpi.phrases:
                problems.append(Problem("KPIs", kpi.row, "phrases", "is blank; the KPI can never be matched"))
            inputs = {p.name.lower() for p in self.parameters}
            for ref in dict.fromkeys(re.findall(r"\{(\w+)\}", kpi.formula)):
                if ref.lower() not in names and ref.lower() not in kpi_names and ref.lower() not in inputs and ref not in ("n", "m"):
                    problems.append(
                        Problem("KPIs", kpi.row, "formula", f"unknown term '{ref}' (a term, a KPI, or a Parameters row of type input)")
                    )
            for required in kpi.requires:
                if required.lower() not in names:
                    problems.append(Problem("KPIs", kpi.row, "requires", f"unknown term '{required}'"))
            if kpi.formula.strip():
                try:
                    sqlglot.parse_one(_placeholders_to_columns(kpi.formula), read=dialect)
                except Exception as exc:
                    problems.append(Problem("KPIs", kpi.row, "formula", f"does not parse: {_short(exc)}"))
            if kpi.reference_sql.strip():
                try:
                    sqlglot.parse_one(_placeholders_to_columns(kpi.reference_sql), read=dialect)
                except Exception as exc:
                    problems.append(Problem("KPIs", kpi.row, "reference_sql", f"does not parse: {_short(exc)}"))
        ids: set[str] = set()
        for example in self.examples:
            if not example.id:
                problems.append(Problem("Examples", example.row, "id", "is blank"))
            elif example.id in ids:
                problems.append(Problem("Examples", example.row, "id", f"'{example.id}' is repeated"))
            ids.add(example.id)
            if not example.question.strip():
                problems.append(Problem("Examples", example.row, "question", "is blank"))
            if not example.correct_sql.strip() and not example.expected_result.strip():
                problems.append(Problem("Examples", example.row, "correct_sql", "give correct_sql or expected_result"))
            if example.correct_sql.strip():
                try:
                    sqlglot.parse_one(_placeholders_to_columns(example.correct_sql), read=dialect)
                except Exception as exc:
                    problems.append(Problem("Examples", example.row, "correct_sql", f"does not parse: {_short(exc)}"))
        for parameter in self.parameters:
            if not parameter.name:
                problems.append(Problem("Parameters", parameter.row, "name", "is blank"))
            elif not re.fullmatch(r"\w+", parameter.name):
                problems.append(Problem("Parameters", parameter.row, "name", f"'{parameter.name}' is not a plain identifier"))
        return problems

    # ---------------------------------------------------------------- matching

    def numeric_parameters(self) -> tuple[str, ...]:
        """Parameters rows of a numeric type: their placeholders bind a number in a phrase, as
        ``{n}`` does, so a KPI may have several numeric slots (``{n_months_back}``, ``{top_n}``)."""
        return tuple(p.name for p in self.parameters if (p.type or "").strip().lower().split("(")[0] in _NUMERIC_PARAMETER_TYPES)

    def match(self, question: str) -> list[Match]:
        """Terms and KPIs whose phrases occur in the question. Exact, after normalising case,
        hyphens, underscores and simple plurals; ``{n}`` binds a number. Longer phrases win
        over shorter ones on the same span."""
        text = normalise(question)
        found: list[Match] = []
        taken: list[tuple[int, int]] = []
        for pattern, name, phrase, kind in self._compiled():
            for m in pattern.finditer(text):
                span = (m.start(), m.end())
                if any(s <= span[0] and span[1] <= e for s, e in taken):
                    continue  # inside a longer phrase already matched
                bindings = {k: v for k, v in m.groupdict().items() if v is not None}
                found.append(Match(name, phrase, span, bindings, kind))
                taken.append(span)
        found.sort(key=lambda x: x.span)
        return found

    def pattern(self, question: str) -> tuple[str, ...]:
        """The question's PATTERN: the sorted set of term and KPI names it matches, closed over
        what the catalogue itself declares — a KPI's ``requires`` terms (saying "headcount" when
        headcount requires ``active`` is saying "active headcount"), and the shorter phrases a
        longer matched phrase contains ("year-over-year active-headcount growth" contains
        "active headcount"). Two questions compare under one reference only when their patterns
        are equal; the closure uses declared facts only, never a guess."""
        text = normalise(question)
        names: set[str] = set()
        for pattern, name, _phrase, _kind in self._compiled():
            if pattern.search(text):
                names.add(name.lower())  # explicit and contained phrases alike
        pending = list(names)
        while pending:
            kpi = self.kpi(pending.pop())
            for required in kpi.requires if kpi else ():
                if required.lower() not in names:
                    names.add(required.lower())
                    pending.append(required.lower())
        # The identifiers a question names (output columns, schema columns: period_seq, active_n,
        # lvl_3_id) are exact tokens, not prose: two questions naming different ones are different
        # questions even when they match the same terms, so they are part of the pattern.
        covered = [m.span for m in self.match(question)]  # an identifier inside a matched phrase is that term
        for token in re.finditer(r"\S+", text):
            word = token.group(0)
            if "_" in word and "{" not in word and not word.startswith("_") and not word.endswith("_"):
                if not any(s <= token.start() and token.end() <= e for s, e in covered):
                    names.add("id:" + word)
        return tuple(sorted(names))

    def _compiled(self) -> list[tuple[re.Pattern[str], str, str, str]]:
        if self._patterns is None:
            entries: list[tuple[re.Pattern[str], str, str, str, int]] = []
            numeric = self.numeric_parameters()
            for term in self.terms:
                phrases = list(term.phrases)
                if term.kind == "source" and term.sql.strip():
                    # A source term names its column: a question that says "assoc_type" names the
                    # source as surely as one that says "employment type" (declared, not guessed).
                    column = term.sql.strip().rpartition(".")[2].lower()
                    if re.fullmatch(r"\w+", column) and column not in {normalise(p).strip() for p in phrases}:
                        phrases.append(column)
                for phrase in phrases:
                    entries.append((_phrase_pattern(phrase, term.qualifiers, numeric), term.name, phrase, term.kind if term.enforceable else "", len(phrase)))
            for kpi in self.kpis:
                for phrase in kpi.phrases:
                    entries.append((_phrase_pattern(phrase, (), numeric), kpi.name, phrase, "kpi" if kpi.enforceable else "", len(phrase)))
            entries.sort(key=lambda e: -e[4])  # longest phrases first
            self._patterns = [(p, n, ph, k) for p, n, ph, k, _ in entries]
        return self._patterns

    def term(self, name: str) -> Term | None:
        return next((t for t in self.terms if t.name.lower() == name.lower()), None)

    def kpi(self, name: str) -> KPI | None:
        return next((k for k in self.kpis if k.name.lower() == name.lower()), None)

    def __len__(self) -> int:
        return len(self.terms) + len(self.kpis)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "terms": len(self.terms),
            "kpis": len(self.kpis),
            "examples": len(self.examples),
            "parameters": [p.name for p in self.parameters],
        }


# ------------------------------------------------------------------ text helpers


def normalise(text: str) -> str:
    """Lower case, one space between tokens, no punctuation. A hyphen splits words ("pay-period"
    is "pay period") except after a negating prefix ("non-deleted" stays one token, so
    "deleted" cannot match inside it); an identifier keeps its underscores ("deleted_at" is one
    token, not the word "deleted")."""
    text = text.lower()
    text = re.sub(r"\b(non|un)-(?=\w)", r"\1", text)
    text = text.replace("-", " ")
    text = re.sub(r"(?<=\d)\.(?=\d)", "\x00", text)  # a decimal point stays ("2.5" is one number)
    text = re.sub(r"[^\w\s{}%\x00]", " ", text).replace("\x00", ".")
    return " " + re.sub(r"\s+", " ", text).strip() + " "


_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
}


_NUMERIC_PARAMETER_TYPES = {"integer", "int", "number", "numeric", "decimal", "float", "percent", "pp", "days", "count"}


def _phrase_pattern(phrase: str, qualifiers: Sequence[str] = (), numeric: Sequence[str] = ()) -> re.Pattern[str]:
    """A phrase becomes a regular expression over the normalised question: words may take a
    plural s, ``{n}``/``{m}`` and every placeholder named in ``numeric`` (a Parameters row of a
    numeric type: ``{n_months_back}``, ``{tolerance_pp}``) take a number — digits, a decimal, or
    a number word — other placeholders take a value. ``qualifiers`` are words the term declares
    a question may put between the phrase's words without changing its meaning ("trailing {n}
    biweekly pay periods")."""
    parts: list[str] = []
    for token in normalise(phrase).split():
        marker = re.fullmatch(r"\{(\w+)\}", token)
        if marker:
            name = marker.group(1)
            if name in ("n", "m") or name in numeric:
                # a number, a number word, or — in a reference's templated question — the
                # reference's own placeholder ("last {n_months} calendar months")
                parts.append(rf"(?P<{name}>\d+(?:\.\d+)?|\{{\w+\}}|{'|'.join(_NUMBER_WORDS)})")
            else:
                # a parameter slot takes a value, not a word: a number, an identifier with digits
                # or underscores, or a quoted/placeholder form — never plain prose
                parts.append(rf"(?P<{name}>\{{\w+\}}|\d[\w.]*|[a-z]+\d[\w.]*|[a-z]*_[\w.]+)")
        else:
            # Singular and plural match each other: "pay periods" in the catalogue finds "pay
            # period window" in a question, and "pay period" finds "pay periods".
            stem = token[:-2] if token.endswith("es") and len(token) > 4 and token[-3] in "sxz" else token[:-1] if token.endswith("s") and len(token) > 3 else token
            parts.append(re.escape(stem) + "(?:s|es)?")
    gap = r"\s+"
    words = [normalise(q).strip() for q in qualifiers if normalise(q).strip()]
    if words:
        gap = r"\s+(?:(?:" + "|".join(re.escape(w) for w in words) + r")\s+)*"
    return re.compile(r"(?<=\s)" + gap.join(parts) + r"(?=\s)")


def bound_number(value: str) -> int | float | None:
    """The number a slot's text stands for: an integer, a decimal ("2.5"), or a number word."""
    value = value.strip().lower()
    if value in _NUMBER_WORDS:
        return int(_NUMBER_WORDS[value])
    try:
        return int(value)
    except ValueError:
        pass
    try:
        number = float(value)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _looks_like_path(value: Any) -> bool:
    if isinstance(value, Path):
        return True
    text = str(value)
    if "\n" in text or len(text) > 400:
        return False
    try:
        return Path(text).exists()
    except OSError:
        return False


def _cell_text(value: Any) -> str:
    """Spreadsheet cells as the text the author typed: no dates, no floats for ids."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _records(rows: list[list[str]], columns: Sequence[str]) -> list[dict[str, Any]]:
    """Header-driven rows into dicts keyed by the canonical column names (case and spaces
    ignored); unknown columns are kept under their own names; row numbers are 1-based."""
    if not rows:
        return []
    header = [c.strip().lower().replace(" ", "_") for c in rows[0]]
    records: list[dict[str, Any]] = []
    for number, row in enumerate(rows[1:], 1):
        if not any((cell or "").strip() for cell in row):
            continue
        record: dict[str, Any] = {"_row": number}
        for key, cell in zip(header, row):
            if key:
                record[key] = (cell or "").strip()
        records.append(record)
    return records


def _split_phrases(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = str(value or "").split(";")
    return tuple(dict.fromkeys(p.strip() for p in items if p and p.strip()))


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _term(row: dict[str, Any], number: int) -> Term:
    dialects = row.get("dialects") if isinstance(row.get("dialects"), dict) else {}
    for key, value in row.items():
        if key.startswith("sql_") and _text(value):  # sql_sqlite, sql_postgres columns
            dialects[key[4:]] = _text(value)
    return Term(
        name=_text(row.get("term") or row.get("name")),
        phrases=_split_phrases(row.get("phrases")),
        kind=_text(row.get("kind")).lower(),
        table=_text(row.get("table") or row.get("applies_to")),
        sql=_text(row.get("sql") or row.get("predicate") or row.get("expression")),
        nulls=_text(row.get("nulls")).lower(),
        description=_text(row.get("description") or row.get("meaning")),
        dialects={k: str(v) for k, v in (dialects or {}).items()},
        row=int(row.get("_row") or number),
        qualifiers=tuple(q.strip().lower() for q in _split_phrases(row.get("qualifiers"))),
    )


def _kpi(row: dict[str, Any], number: int) -> KPI:
    return KPI(
        name=_text(row.get("kpi") or row.get("name")),
        phrases=_split_phrases(row.get("phrases")),
        formula=_text(row.get("formula")),
        format=_text(row.get("format")).lower(),
        requires=_split_phrases(row.get("requires")),
        reference_sql=_text(row.get("reference_sql")),
        description=_text(row.get("description")),
        row=int(row.get("_row") or number),
    )


def _example(row: dict[str, Any], number: int) -> Example:
    return Example(
        id=_text(row.get("id")),
        question=_text(row.get("question")),
        params=_text(row.get("params")),
        correct_sql=_text(row.get("correct_sql") or row.get("sql")),
        expected_result=_text(row.get("expected_result")),
        dataset=_text(row.get("dataset")),
        row=int(row.get("_row") or number),
    )


def _parameter(row: dict[str, Any], number: int) -> Parameter:
    return Parameter(
        name=_text(row.get("name")),
        type=_text(row.get("type")),
        example_values=_text(row.get("example_values")),
        row=int(row.get("_row") or number),
    )


# ------------------------------------------------------------ the sql of a term

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _placeholders_to_columns(sql: str) -> str:
    """``{n}`` and ``{param}`` become identifiers so the text parses."""
    return _PLACEHOLDER.sub(lambda m: f"sqlgami_ph_{m.group(1)}", sql)


def _short(exc: Exception) -> str:
    return " ".join(str(exc).split())[:160]


def parse_term_sql(term: Term, dialect: str) -> exp.Expression | None:
    """The term's sql as a sqlglot expression: a condition for ``filter`` and ``window`` (a frame
    becomes a Window spec), a value expression for ``formula``, a column for ``source``, and a
    tuple of columns compared to a value for ``chain``."""
    sql = term.sql_for(dialect).strip()
    if not sql:
        return None
    text = _placeholders_to_columns(sql)
    if term.kind in ("filter", "window"):
        if re.match(r"(?i)\s*(rows|range)\s+between", text):
            return sqlglot.parse_one(f"SELECT COUNT(*) OVER ({text}) FROM t", read=dialect).find(exp.WindowSpec)
        return sqlglot.parse_one(f"SELECT 1 FROM t WHERE {text}", read=dialect).args["where"].this
    if term.kind == "formula":
        return sqlglot.parse_one(f"SELECT {text} AS v FROM t", read=dialect).expressions[0].this
    if term.kind == "source":
        return sqlglot.parse_one(f"SELECT {text} FROM t", read=dialect).expressions[0]
    if term.kind == "rank":
        rule = rank_rule(sql)
        if rule is None:
            raise ValueError("a rank is written as 'LIMIT {n}', 'DENSE_RANK() <= {n}', 'RANK() <= {n}' or 'ROW_NUMBER() <= {n}'")
        return exp.Tuple(expressions=[exp.Literal.string(rule[0]), exp.Literal.string(rule[1])])
    if term.kind == "chain":
        left, _, right = text.rpartition("=")
        columns = [c.strip() for c in left.split(",") if c.strip()]
        if not columns or not right.strip():
            raise ValueError("a chain is written as 'col1, col2, ... = {param}'")
        return sqlglot.parse_one(f"SELECT 1 FROM t WHERE ({', '.join(columns)}) = ({right.strip()})", read=dialect).args["where"].this
    return None


def rank_rule(sql: str) -> tuple[str, str] | None:
    """How a ``rank`` term cuts the top N: ``("limit" | "rank" | "dense_rank" | "row_number",
    bound)`` where the bound is a number or a ``{slot}``; None when the text is not one of those."""
    text = sql.strip()
    m = re.fullmatch(r"(?is)limit\s+(\{\w+\}|\d+)", text)
    if m:
        return "limit", m.group(1)
    m = re.fullmatch(r"(?is)(rank|dense_rank|row_number)\s*\(\s*\)\s*(<=|<)\s*(\{\w+\}|\d+)", text)
    if m:
        bound = m.group(3)
        if m.group(2) == "<":
            if not bound.isdigit():
                return None  # "< {n}" is one less than n: write "<= {n}" over a slot
            bound = str(int(bound) - 1)
        return m.group(1).lower(), bound
    return None


def _check_term_sql(term: Term, dialect: str) -> list[str]:
    try:
        node = parse_term_sql(term, dialect)
    except Exception as exc:
        return [f"does not parse: {_short(exc)}"]
    if node is None:
        return ["is blank"]
    if term.kind == "source":
        column = node if isinstance(node, exp.Column) else None
        if column is None or not column.table:
            return ["a source is written as table.column"]
    if term.kind == "window":
        if isinstance(node, exp.WindowSpec):
            return []
        if not any(isinstance(n, (exp.Between, exp.EQ, exp.GTE, exp.GT, exp.LTE, exp.LT)) for n in node.walk()):
            return ["a window is a range or point on a column (BETWEEN, =, >=) or a ROWS BETWEEN frame"]
    return []


def catalogue_from_yaml_glossary(text: str) -> Catalogue | None:
    """A bench glossary file may carry a ``terms:`` section next to its prose; return it if so."""
    try:
        import yaml

        data = yaml.safe_load(text)
    except Exception:
        return None
    if isinstance(data, dict) and isinstance(data.get("terms"), list):
        return Catalogue.from_dict({k: data.get(k) for k in ("terms", "kpis", "examples", "parameters")})
    return None


def load_terms(source: Any, dialect: str = "duckdb") -> Catalogue:
    """``Scorer(terms=...)``: see :meth:`Catalogue.load`. Validation problems are errors; the
    SQL in the sheets is read in ``dialect`` (a BigQuery host writes backticked names)."""
    catalogue = Catalogue.load(source)
    problems = catalogue.validate(dialect)
    if problems:
        shown = "; ".join(str(p) for p in problems[:5])
        more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        raise ConfigurationError(f"the term catalogue has problems: {shown}{more}")
    return catalogue
