"""The optional Hugging Face generator, with the network replaced by a stub."""

from __future__ import annotations

import subprocess
import sys

import pytest

from sqlgami import ConfigurationError, Scorer
from sqlgami.contrib.hf import GeneratorError, HFGenerator, check_model, extract_sql

from conftest import make_db

TOKEN = "hf_test_token_that_must_never_appear_anywhere"


def test_allowlist():
    for model in (
        "meta-llama/Llama-3.3-70B-Instruct",
        "google/gemma-3-27b-it",
        "mistralai/Codestral-22B-v0.1",
        "microsoft/phi-4",
        "ibm-granite/granite-3.3-8b-instruct",
        "openai/gpt-oss-120b",
        "nvidia/Nemotron-4-340B-Instruct",
    ):
        assert check_model(model) == model
    for model in (
        "",
        "Qwen/Qwen2.5-Coder-32B-Instruct",
        "deepseek-ai/DeepSeek-R1",
        "meta-llama/DeepSeek-R1-Distill-Llama-70B",
        "mistralai/qwen-finetune",
        "zai-org/GLM-4.6",
        "seeklhy/OmniSQL-32B",
        "some-org/some-model",
        "google/flan-t5",
    ):
        with pytest.raises(ConfigurationError):
            check_model(model)


def test_token_is_required_and_never_shown(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(ConfigurationError):
        HFGenerator("meta-llama/Llama-3.3-70B-Instruct", dialect="duckdb")
    monkeypatch.setenv("HF_TOKEN", TOKEN)
    generator = HFGenerator("meta-llama/Llama-3.3-70B-Instruct", dialect="duckdb")
    assert TOKEN not in repr(generator) and TOKEN not in str(vars(generator).get("model"))

    def failing(url, headers, payload, timeout):
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        raise GeneratorError("the model provider answered with HTTP 401")

    broken = HFGenerator("google/gemma-3-27b-it", dialect="duckdb", transport=failing)
    with pytest.raises(GeneratorError) as caught:
        broken("q", "schema", None, 1)
    assert TOKEN not in str(caught.value)


def test_extract_sql():
    assert extract_sql("```sql\nSELECT 1;\n```") == "SELECT 1"
    assert extract_sql("Here you go:\n```\nWITH x AS (SELECT 1 AS a) SELECT a FROM x\n```\nHope it helps") == (
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x"
    )
    assert extract_sql("The answer is SELECT name FROM customers; DROP TABLE x;") == "SELECT name FROM customers"
    assert extract_sql("I cannot answer that.") is None
    assert extract_sql("") is None


def test_generator_in_full_mode(engine, db):
    replies = iter(
        [
            "```sql\nSELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name\n```",
            "WITH t AS (SELECT customer_id, SUM(total) s FROM orders GROUP BY customer_id) "
            "SELECT c.name, t.s FROM customers c JOIN t ON t.customer_id = c.id;",
            "Sorry, I am not sure.",
        ]
    )
    seen = []

    def transport(url, headers, payload, timeout):
        seen.append(payload)
        return {"choices": [{"message": {"content": next(replies)}}]}

    generator = HFGenerator("meta-llama/Llama-3.3-70B-Instruct", dialect=engine, token=TOKEN, transport=transport)
    candidate = (
        "SELECT c.name, (SELECT SUM(o.total) FROM orders o WHERE o.customer_id = c.id) FROM customers c "
        "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)"
    )
    with Scorer(engine, db=db, generator=generator) as scorer:
        report = scorer.score(candidate, "Total order value per customer", mode="full")
    assert report.signals["consistency"]["agreeing"] == 2 and report.score >= 0.9
    assert len(seen) == 3 and seen[0]["temperature"] == 0.0 and seen[1]["temperature"] > 0
    assert seen[0]["model"] == "meta-llama/Llama-3.3-70B-Instruct"
    prompt = seen[0]["messages"][1]["content"]
    assert "customers" in prompt and "Total order value per customer" in prompt
    assert TOKEN not in report.to_json()


def test_provider_failure_is_reported_not_raised(engine, db):
    def transport(url, headers, payload, timeout):
        raise GeneratorError("the model provider answered with HTTP 503")

    generator = HFGenerator("google/gemma-3-27b-it", dialect=engine, token=TOKEN, transport=transport)
    with Scorer(engine, db=db, generator=generator) as scorer:
        report = scorer.score("SELECT name FROM customers ORDER BY name", "List customer names, sorted", mode="full")
    assert report.stage("consistency").status.value == "skipped"
    assert TOKEN not in report.to_json()


def test_core_import_does_not_load_the_network_client():
    code = "import sys, sqlgami; print('sqlgami.contrib.hf' in sys.modules, 'urllib.request' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    assert out == ["False", "False"]
