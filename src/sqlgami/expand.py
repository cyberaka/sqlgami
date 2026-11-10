"""Offline expansion of a term catalogue with a language model (ADR 0002): synonym phrases for
terms and KPIs, and paraphrased reference questions with other slot values.

Nothing enters the bundle on the model's word. A proposed phrase is kept only when, added to
the catalogue, it changes the pattern of no existing question and the questions that use the
term re-match to the same terms and slots with the new wording in place. A proposed variant is
kept only when it re-matches deterministically to its reference's pattern, binds the same slots
(the model's own claim about the values must agree with what the text binds), states the same
free numbers, and — when its values differ — the reference is written over those slots; the
compile step then runs the reference at those values on every fixture before storing it. Every
other proposal goes to ``rejected.jsonl`` with the reason. Accepted phrases are written as a
review file for the host to accept into the workbook: the catalogue the live path reads is the
host's, and the live path never calls a model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .bundle import Reference, free_numbers, slot_values
from .terms import Catalogue, Term, _phrase_pattern, normalise

# A chat call: messages in, the reply text out. Offline only.
Chat = Callable[[list[dict[str, str]]], str]

MAX_PHRASE_WORDS = 7


@dataclass
class AcceptedPhrase:
    term: str
    phrase: str
    evidence: str  # the question (its id) that re-matched with the phrase in place

    def to_dict(self) -> dict[str, Any]:
        return {"term": self.term, "phrase": self.phrase, "evidence": self.evidence}


@dataclass
class Expansion:
    model: str
    phrases: list[AcceptedPhrase] = field(default_factory=list)
    variants: list[Reference] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    proposals: int = 0

    def review_csv(self) -> str:
        """A Terms-sheet addition the host can review: one row per accepted phrase."""
        lines = ["term,phrase,evidence"]
        for p in self.phrases:
            lines.append(",".join(_csv(v) for v in (p.term, p.phrase, p.evidence)))
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "proposals": self.proposals,
            "phrases": [p.to_dict() for p in self.phrases],
            "variants": [{"key": v.key, "variant_of": v.variant_of, "question": v.question, "slots": v.slots} for v in self.variants],
            "rejected": len(self.rejected),
        }


def _csv(value: str) -> str:
    if any(c in value for c in ',"\n'):
        return '"' + value.replace('"', '""') + '"'
    return value


# ------------------------------------------------------------------ proposals


def _json_list(text: str) -> list[Any]:
    """The first JSON array in a reply, or []."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def propose_phrases(chat: Chat, catalogue: Catalogue, name: str, k: int) -> list[str]:
    term = catalogue.term(name)
    kpi = catalogue.kpi(name) if term is None else None
    if term is None and kpi is None:
        return []
    phrases = list(term.phrases if term else kpi.phrases)  # type: ignore[union-attr]
    description = (term.description if term else kpi.description).strip()  # type: ignore[union-attr]
    slots = sorted({s for p in phrases for s in re.findall(r"\{(\w+)\}", p)})
    system = (
        "You propose alternative English wordings for a business term, for exact phrase matching in "
        "questions people type. Reply with a JSON array of strings and nothing else."
    )
    user = [
        f"Term: {name}",
        f"Meaning: {description or '(not described)'}",
        "Existing wordings: " + "; ".join(phrases),
    ]
    if slots:
        user.append(
            "Placeholders: " + ", ".join("{" + s + "}" for s in slots)
            + " stand for a number the question states and must appear, spelled exactly like that, in every wording you propose."
        )
    user.append(
        f"Propose up to {k} further wordings a person might use for the same term in a question: lower case, "
        f"at most {MAX_PHRASE_WORDS} words, no punctuation, no new placeholders, none of the existing wordings, "
        "and nothing so generic that it would also appear in questions not about this term."
    )
    reply = chat([{"role": "system", "content": system}, {"role": "user", "content": "\n".join(user)}])
    return [str(x).strip() for x in _json_list(reply) if isinstance(x, str) and x.strip()][: max(0, k)]


def propose_variants(chat: Chat, reference: Reference, k: int, slot_choices: dict[str, Sequence[Any]]) -> list[dict[str, Any]]:
    system = (
        "You rewrite a database question in other words a person might use, keeping its meaning exactly: "
        "the same measure, the same filters, the same output columns and order. Reply with a JSON array of "
        'objects {"question": "...", "slots": {...}} and nothing else.'
    )
    user = [f"Question: {reference.question}"]
    if reference.slots:
        user.append("Numbers it binds: " + ", ".join(f"{s} = {v}" for s, v in reference.slots.items()))
        choices = {s: [c for c in slot_choices.get(s, []) if c != reference.slots.get(s)] for s in reference.slots}
        if any(choices.values()):
            user.append(
                "In about half of the rewrites use another value for a number, chosen from: "
                + "; ".join(f"{s}: {list(v)}" for s, v in choices.items() if v)
                + ". State the value in the text in words or digits and report it in \"slots\"."
            )
        else:
            user.append('Keep the numbers as they are and report them in "slots".')
    else:
        user.append('There are no numbers to vary; "slots" is {}.')
    user.append(f"Write {k} rewrites. Do not add, drop or change any condition, column or ordering.")
    reply = chat([{"role": "system", "content": system}, {"role": "user", "content": "\n".join(user)}])
    found: list[dict[str, Any]] = []
    for item in _json_list(reply):
        if isinstance(item, dict) and isinstance(item.get("question"), str) and item["question"].strip():
            slots = item.get("slots") if isinstance(item.get("slots"), dict) else {}
            found.append({"question": item["question"].strip(), "slots": {str(s): v for s, v in slots.items()}})
    return found[: max(0, k)]


# ------------------------------------------------------------------ acceptance


def _with_phrase(catalogue: Catalogue, name: str, phrase: str) -> Catalogue:
    terms = [Term(t.name, t.phrases + ((phrase,) if t.name.lower() == name.lower() else ()), t.kind, t.table, t.sql, t.nulls, t.description, dict(t.dialects), t.row) for t in catalogue.terms]
    kpis = []
    for k in catalogue.kpis:
        copy = type(k)(k.name, k.phrases + ((phrase,) if k.name.lower() == name.lower() else ()), k.formula, k.format, k.requires, k.reference_sql, k.description, k.row)
        kpis.append(copy)
    return Catalogue(terms, kpis, list(catalogue.examples), list(catalogue.parameters), catalogue.source)


def _placeholders(phrase: str) -> frozenset[str]:
    return frozenset(re.findall(r"\{(\w+)\}", phrase))


def _render(phrase: str, bindings: dict[str, str]) -> str:
    text = phrase
    for slot, value in bindings.items():
        text = text.replace("{" + slot + "}", value)
    return text


def accept_phrase(
    catalogue: Catalogue, name: str, phrase: str, questions: Sequence[tuple[str, str]]
) -> tuple[Catalogue | None, str]:
    """``(catalogue with the phrase, evidence)`` when the phrase is safe, else ``(None, reason)``.
    ``questions`` are ``(id, text)`` pairs: every registered question must keep its pattern and
    slots, and one that uses the term must re-match with the new wording in its place."""
    term = catalogue.term(name)
    kpi = catalogue.kpi(name) if term is None else None
    existing = list(term.phrases if term else kpi.phrases) if (term or kpi) else []  # type: ignore[union-attr]
    if not existing:
        return None, "unknown term"
    words = normalise(phrase).split()
    if not words:
        return None, "empty"
    if len(words) > MAX_PHRASE_WORDS:
        return None, f"longer than {MAX_PHRASE_WORDS} words"
    if _placeholders(phrase) not in {_placeholders(p) for p in existing}:
        return None, "placeholders differ from the term's existing wordings"
    if re.search(r"[^\w\s{}'/-]", phrase):
        return None, "contains punctuation"
    normal = normalise(phrase).strip()
    for other in list(catalogue.terms) + list(catalogue.kpis):
        if any(normalise(p).strip() == normal for p in other.phrases):
            return None, f"already a wording of {other.name}" if other.name.lower() != name.lower() else "already a wording of the term"
    try:
        _phrase_pattern(phrase)
    except re.error:
        return None, "does not compile"
    trial = _with_phrase(catalogue, name, phrase)
    pattern = _phrase_pattern(phrase)
    # No registered question may change its pattern or its slots, and the wording must not already
    # occur in one where it does not stand for the term ("customers" offered for "paid").
    for qid, text in questions:
        if trial.pattern(text) != catalogue.pattern(text):
            return None, f"changes the pattern of {qid}"
        if slot_values(trial.match(text)) != slot_values(catalogue.match(text)):
            return None, f"changes the slots of {qid}"
        normal_text = normalise(text)
        own = [m.span for m in catalogue.match(text) if m.name.lower() == name.lower()]
        for hit in pattern.finditer(normal_text):
            if not any(s <= hit.start() and hit.end() <= e for s, e in own):
                return None, f"occurs in {qid} where it does not stand for the term"
    # A question that uses the term must re-match with the new wording in place of the old.
    for qid, text in questions:
        normal_text = normalise(text)
        for m in catalogue.match(text):
            if m.name.lower() != name.lower():
                continue
            substituted = normal_text[: m.span[0]] + _render(phrase, m.bindings) + normal_text[m.span[1] :]
            if trial.pattern(substituted) == catalogue.pattern(text) and slot_values(trial.match(substituted)) == slot_values(catalogue.match(text)):
                return trial, qid
            return None, f"{qid} reworded with it does not re-match the same terms and slots"
    # No question uses the term: the wording on its own must mean the term and nothing else.
    sample = {s: "3" for s in _placeholders(phrase)}
    alone, original = _render(phrase, sample), _render(existing[0], sample)
    if trial.pattern(alone) == catalogue.pattern(original) and slot_values(trial.match(alone)) == slot_values(catalogue.match(original)):
        return trial, "(no registered question uses the term; the wording alone matches it)"
    return None, "on its own it does not match the term alone"


def accept_variant(
    catalogue: Catalogue, reference: Reference, question: str, claimed: dict[str, Any], taken: set[str]
) -> tuple[Reference | None, str]:
    """A paraphrase becomes a variant reference only when the text re-matches the reference's
    pattern and binds its slots (as the model claimed), states the same free numbers, and the
    reference can be run at those values."""
    key = normalise(question).strip()
    if not key:
        return None, "empty"
    if key == reference.question_key or key in taken:
        return None, "duplicate"
    if catalogue.pattern(question) != tuple(reference.terms):
        return None, "does not re-match the reference's pattern"
    bound = slot_values(catalogue.match(question))
    if set(bound) != set(reference.slots):
        return None, "does not bind the reference's slots"
    claimed_numbers = {}
    for s, v in (claimed or {}).items():
        try:
            claimed_numbers[s] = int(v)
        except (TypeError, ValueError):
            return None, "claimed slot values are not numbers"
    if claimed_numbers and claimed_numbers != {s: int(v) for s, v in bound.items()}:
        return None, f"claims slots {claimed_numbers} but the text binds {bound}"
    if free_numbers(question, bound, reference.params) != free_numbers(reference.question, reference.slots, reference.params):
        return None, "states numbers the reference does not"
    differing = {s for s, v in bound.items() if reference.slots.get(s) != v}
    if differing - reference.slot_names_in_sql:
        return None, f"the reference is fixed at {', '.join(f'{s}={reference.slots.get(s)}' for s in sorted(differing))}"
    variant = Reference(f"{reference.key}~{len(taken) + 1}", question, reference.sql, dict(reference.params), tuple(reference.terms), bound)
    variant.variant_of = reference.key
    return variant, "accepted"


# ------------------------------------------------------------------ driver


def expand(
    catalogue: Catalogue,
    references: Sequence[Reference],
    chat: Chat,
    *,
    model: str = "",
    per_term: int = 6,
    per_example: int = 4,
    slot_choices: dict[str, Sequence[Any]] | None = None,
) -> Expansion:
    """Propose and validate phrases for every term and KPI, then variants for every reference,
    against the catalogue with the accepted phrases in place. Returns what was accepted, what was
    rejected and why. Variants come back without fixture results: the compile step runs them."""
    result = Expansion(model=model)
    questions: list[tuple[str, str]] = [(e.id, e.question) for e in catalogue.examples]
    for r in references:
        if all(q[1] != r.question for q in questions):
            questions.append((r.key, r.question))
    current = catalogue
    if per_term > 0:
        for name in [t.name for t in catalogue.terms if t.phrases] + [k.name for k in catalogue.kpis if k.phrases]:
            try:
                proposed = propose_phrases(chat, current, name, per_term)
            except Exception as exc:  # the model is optional help; a failed call is a rejection, not an error
                result.rejected.append({"kind": "phrase", "term": name, "text": "", "reason": f"model call failed: {str(exc)[:160]}"})
                continue
            for phrase in proposed:
                result.proposals += 1
                trial, reason = accept_phrase(current, name, phrase, questions)
                if trial is None:
                    result.rejected.append({"kind": "phrase", "term": name, "text": phrase, "reason": reason})
                else:
                    current = trial
                    result.phrases.append(AcceptedPhrase(name, phrase, reason))
    if per_example > 0:
        choices = dict(slot_choices or {})
        for reference in references:
            if reference.variant_of:
                continue
            try:
                proposed = propose_variants(chat, reference, per_example, choices)
            except Exception as exc:
                result.rejected.append({"kind": "variant", "reference": reference.key, "text": "", "reason": f"model call failed: {str(exc)[:160]}"})
                continue
            taken = {v.question_key for v in result.variants if v.variant_of == reference.key}
            for item in proposed:
                result.proposals += 1
                variant, reason = accept_variant(current, reference, item["question"], item.get("slots") or {}, taken)
                if variant is None:
                    result.rejected.append({"kind": "variant", "reference": reference.key, "text": item["question"], "slots": item.get("slots"), "reason": reason})
                else:
                    taken.add(variant.question_key)
                    result.variants.append(variant)
    result.catalogue = current  # type: ignore[attr-defined]  # the catalogue with the accepted phrases, for tests and reports
    return result


def chat_for(spec: str, **kwargs: Any) -> Chat:
    """A chat callable from a model spec: ``hf:<model>`` (Hugging Face router, HF_TOKEN) or
    ``claude:<model>`` (the Anthropic SDK, ANTHROPIC_API_KEY). Offline use only."""
    provider, _, model = spec.partition(":")
    if provider == "hf" and model:
        from .contrib.hf import HFChat

        return HFChat(model, **kwargs)
    if provider == "claude" and model:
        from .contrib.hf import ClaudeChat

        return ClaudeChat(model, **kwargs)
    from .errors import ConfigurationError

    raise ConfigurationError(f"unknown model spec {spec!r}: use hf:<model> or claude:<model>")
