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

    def __call__(self, question: str, schema: str, glossary: str | None, k: int) -> Sequence[str]:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        messages = self._messages(question, schema, glossary)
        found: list[str] = []
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
            if sql and sql not in found:
                found.append(sql)
        return found
