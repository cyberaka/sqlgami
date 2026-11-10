"""A generator for the consistency stage, backed by Hugging Face Inference.

    from sqlgami import Scorer
    from sqlgami.contrib.hf import HFGenerator

    generator = HFGenerator("meta-llama/Llama-3.3-70B-Instruct", dialect="postgres")
    scorer = Scorer("postgres", db=conn, generator=generator)
    report = scorer.score(sql, question, mode="full")

Importing this module makes no network call; calling the generator does. Each call
sends the question, a description of your schema and your glossary to the provider.
Do not use it where that is not acceptable.

sqlgami ships no default model. The model must be named, and must come from the
allowlist below. The access token is read from the ``HF_TOKEN`` environment variable
or passed in; it is never logged, printed or placed in an error message.

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Sequence

from ..errors import ConfigurationError, SqlgamiError

DEFAULT_ENDPOINT = "https://router.huggingface.co/v1/chat/completions"

# Model families that may be used. Anything else is refused.
ALLOWED_PREFIXES = (
    "meta-llama/",
    "google/gemma",
    "mistralai/",
    "microsoft/phi",
    "ibm-granite/",
    "openai/gpt-oss",
    "nvidia/",
)
# Refused even under an allowed prefix: models, fine-tunes and distillations of these families.
BLOCKED_MARKERS = (
    "qwen",
    "deepseek",
    "glm",
    "chatglm",
    "yi-",
    "kimi",
    "moonshot",
    "minimax",
    "baichuan",
    "internlm",
    "hunyuan",
    "ernie",
    "xiyan",
    "omnisql",
)

Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


class GeneratorError(SqlgamiError):
    """The model provider could not be reached or returned something unusable."""


def check_model(model: str) -> str:
    """Return ``model`` if it is allowed, else raise :class:`ConfigurationError`."""
    name = (model or "").strip()
    lowered = name.lower()
    if not name:
        raise ConfigurationError("a model must be named; sqlgami has no default model")
    if any(marker in lowered for marker in BLOCKED_MARKERS):
        raise ConfigurationError(f"model {name!r} is not on the allowlist")
    if not any(lowered.startswith(prefix) for prefix in ALLOWED_PREFIXES):
        raise ConfigurationError(
            f"model {name!r} is not on the allowlist; allowed families: {', '.join(ALLOWED_PREFIXES)}"
        )
    return name


def _post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - fixed https endpoint
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The response body and the request headers are deliberately left out.
        raise GeneratorError(f"the model provider answered with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GeneratorError(f"the model provider could not be reached ({type(exc).__name__})") from None
    except ValueError:
        raise GeneratorError("the model provider returned something that is not JSON") from None


_FENCE = re.compile(r"```(?:sql|[a-z]*)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_STARTS = re.compile(r"\b(with|select)\b", re.IGNORECASE)


def extract_sql(text: str) -> str | None:
    """Pull one SQL statement out of a model's reply."""
    if not text:
        return None
    fenced = _FENCE.search(text)
    body = fenced.group(1) if fenced else text
    start = _STARTS.search(body)
    if not start:
        return None
    body = body[start.start():].strip()
    end = body.find(";")
    if end != -1:
        body = body[:end]
    body = body.strip()
    return body or None


class HFGenerator:
    """Writes alternative SQL for a question. Usable as ``Scorer(generator=...)``."""

    def __init__(
        self,
        model: str,
        *,
        dialect: str,
        token: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        temperature: float = 0.7,
        max_tokens: int = 900,
        timeout_s: float = 60.0,
        transport: Transport | None = None,
    ) -> None:
        self.model = check_model(model)
        self.dialect = dialect
        self.endpoint = endpoint
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self._transport = transport or _post
        self._token = token or os.environ.get("HF_TOKEN") or ""
        if not self._token:
            raise ConfigurationError("no access token: set HF_TOKEN in the environment or pass token=")

    def __repr__(self) -> str:
        return f"HFGenerator(model={self.model!r}, dialect={self.dialect!r})"

    def _messages(self, question: str, schema: str, glossary: str | None) -> list[dict[str, str]]:
        system = (
            f"You write one {self.dialect} SQL query that answers the user's question. "
            "Use only the tables and columns in the schema. Reply with the SQL only, no explanation."
        )
        parts = [f"Schema:\n{schema}"]
        if glossary:
            parts.append(f"Definitions:\n{glossary}")
        parts.append(f"Question: {question}")
        return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]

    def __call__(self, question: str, schema: str, glossary: str | None, k: int) -> Sequence[tuple[str, str]]:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        messages = self._messages(question, schema, glossary)
        found: list[tuple[str, str]] = []
        for index in range(max(1, k)):
            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": self.max_tokens,
                # The first attempt is the model's best answer; later ones vary.
                "temperature": 0.0 if index == 0 else self.temperature,
            }
            reply = self._transport(self.endpoint, headers, payload, self.timeout_s)
            try:
                text = reply["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise GeneratorError("the model provider returned an unexpected reply") from None
            sql = extract_sql(text if isinstance(text, str) else "")
            if sql and sql not in [f[0] for f in found]:
                found.append((sql, self.model))  # tagged, so samples of one model count as one voice
        return found


_PROBE_RELATIONS = """
equals_row_count      -> the value equals the number of rows the answer has
equals_sum            -> the value equals SUM(<column>) over the answer's rows (or those matching "filter")
equals_count_distinct -> the value equals COUNT(DISTINCT <column>) over the answer's rows
equals_value          -> the value equals <column> in the single answer row matching "filter"
equals_max / equals_min -> the value equals the largest / smallest <column> in the answer
upper_bound / lower_bound -> every <column> in the answer is <= / >= the value
""".strip()


class HFProber:
    """Asks a model for simple anchor queries with a declared relation to the answer. Usable as
    ``Scorer(prober=...)``. Same model rules and token handling as :class:`HFGenerator`."""

    def __init__(
        self,
        model: str,
        *,
        dialect: str,
        token: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        max_tokens: int = 1200,
        timeout_s: float = 60.0,
        transport: Callable[..., dict[str, Any]] | None = None,
        probes: int = 3,
    ) -> None:
        self.model = check_model(model)
        self.dialect = dialect
        self.endpoint = endpoint
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.probes = probes
        self._transport = transport or _post
        self._token = token or os.environ.get("HF_TOKEN") or ""
        if not self._token:
            raise ConfigurationError("no access token: set HF_TOKEN in the environment or pass token=")

    def __call__(self, question: str, schema: str, glossary: str | None, columns: Sequence[str]) -> list[Any]:
        system = (
            f"You check another analyst's {self.dialect} SQL answer to a question by writing SIMPLE anchor queries. "
            "Each anchor query must return exactly one value (one row, one column) and come with the relation that "
            "value must have to the answer. Prefer totals over all rows, row counts, and distinct counts of key "
            "columns. Use only the tables and columns in the schema. Do not rewrite the whole question. "
            f"Reply with a JSON array only, {self.probes} items at most, each an object with keys "
            '"probe_sql", "relation", "column" (an answer column name, or null for equals_row_count), '
            '"filter" (an object {answer column: value} selecting answer rows, or null) and "description".\n'
            "Relations:\n" + _PROBE_RELATIONS
        )
        parts = [f"Schema:\n{schema}"]
        if glossary:
            parts.append(f"Definitions:\n{glossary}")
        parts.append(f"Question: {question}")
        parts.append("The answer's columns, in order: " + ", ".join(columns))
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}],
            "max_tokens": self.max_tokens,
            "temperature": 0.0,
        }
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        reply = self._transport(self.endpoint, headers, payload, self.timeout_s)
        try:
            text = reply["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise GeneratorError("the model provider returned an unexpected reply") from None
        from ..reconcile import extract_json, parse_specs

        return parse_specs(extract_json(text if isinstance(text, str) else ""), source=self.model)


# ------------------------------------------------------------------- offline chat


class HFChat:
    """A plain chat call for the offline compile step (``sqlgami.expand``): messages in, reply
    text out. Same endpoint, allowlist and token rules as :class:`HFGenerator`."""

    def __init__(
        self,
        model: str,
        *,
        token: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        temperature: float = 0.3,
        max_tokens: int = 1200,
        timeout_s: float = 90.0,
        transport: Transport | None = None,
    ) -> None:
        self.model = check_model(model)
        self.endpoint = endpoint
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self._transport = transport or _post
        self._token = token or os.environ.get("HF_TOKEN") or ""
        if not self._token:
            raise ConfigurationError("no access token: set HF_TOKEN in the environment or pass token=")

    def __repr__(self) -> str:
        return f"HFChat(model={self.model!r})"

    def __call__(self, messages: list[dict[str, str]]) -> str:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        payload = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens, "temperature": self.temperature}
        reply = self._transport(self.endpoint, headers, payload, self.timeout_s)
        try:
            text = reply["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise GeneratorError("the model provider returned an unexpected reply") from None
        return text if isinstance(text, str) else ""


class ClaudeChat:
    """The same, through the Anthropic SDK (``pip install anthropic``; ``ANTHROPIC_API_KEY``)."""

    def __init__(self, model: str, *, temperature: float = 0.3, max_tokens: int = 1200) -> None:
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ConfigurationError("claude:<model> needs the anthropic package: pip install anthropic") from exc
        if not model.lower().startswith("claude"):
            raise ConfigurationError(f"model {model!r} is not a Claude model")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic()

    def __repr__(self) -> str:
        return f"ClaudeChat(model={self.model!r})"

    def __call__(self, messages: list[dict[str, str]]) -> str:
        system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        rest = [m for m in messages if m.get("role") != "system"]
        reply = self._client.messages.create(model=self.model, max_tokens=self.max_tokens, temperature=self.temperature, system=system or None, messages=rest)
        return "".join(getattr(block, "text", "") for block in reply.content)
