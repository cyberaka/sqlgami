"""The offline BigQuery adapter: the project guard runs before any client exists; live calls only
when SQLGAMI_BQ_LIVE=1 and the sandbox is reachable (ADR 0003, B2)."""

from __future__ import annotations

import os

import pytest

from sqlgami.contrib.bigquery import DATASET_PREFIX, dataset_name, sandbox_project
from sqlgami.errors import ConfigurationError


def test_only_the_sandbox_project_is_ever_accepted(monkeypatch):
    monkeypatch.delenv("SQLGAMI_BQ_PROJECT", raising=False)
    with pytest.raises(ConfigurationError, match="no BigQuery project"):
        sandbox_project()
    for bad in ("my-prod-project", "sqlgami-prod", "sqlgami-dev-1109x", "", "prod-sqlgami-dev-1109"):
        monkeypatch.setenv("SQLGAMI_BQ_PROJECT", bad)
        with pytest.raises(ConfigurationError, match="refusing|no BigQuery project"):
            sandbox_project()
    monkeypatch.setenv("SQLGAMI_BQ_PROJECT", "sqlgami-dev-1109")
    assert sandbox_project() == "sqlgami-dev-1109"
    assert sandbox_project("sqlgami-dev-1109") == "sqlgami-dev-1109"
    with pytest.raises(ConfigurationError):
        sandbox_project("sqlgami-other")
    assert dataset_name(f"{DATASET_PREFIX}hr_seed47") == "bench_hr_seed47"
    with pytest.raises(ConfigurationError, match="must start with"):
        dataset_name("hr_seed47")


def test_the_client_is_never_built_for_a_refused_project(monkeypatch):
    """Even with the package installed, a wrong project never reaches google.cloud.bigquery."""
    import sys
    import types

    from sqlgami.contrib import bigquery as module

    built = []
    fake = types.SimpleNamespace(Client=lambda project: built.append(project))
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake)
    monkeypatch.setitem(sys.modules, "google.cloud", types.SimpleNamespace(bigquery=fake))
    monkeypatch.setitem(sys.modules, "google", types.SimpleNamespace(cloud=sys.modules["google.cloud"]))
    with pytest.raises(ConfigurationError):
        module.BigQuerySandbox("some-other-project")
    assert built == []


@pytest.mark.skipif(os.environ.get("SQLGAMI_BQ_LIVE") != "1", reason="live sandbox checks run only with SQLGAMI_BQ_LIVE=1")
def test_live_sandbox_dry_run_and_query(tmp_path):
    from sqlgami.contrib.bigquery import BigQuerySandbox

    sandbox = BigQuerySandbox(ledger=tmp_path / "ledger.jsonl")
    assert sandbox.project == "sqlgami-dev-1109"
    priced = sandbox.dry_run("SELECT 1 AS one, DATE_SUB(DATE '2024-03-15', INTERVAL 1 DAY) AS d")
    assert priced.ok and priced.columns == ["one", "d"]
    result = sandbox.run("SELECT 1 AS one, DATE_SUB(DATE '2024-03-15', INTERVAL 1 DAY) AS d")
    assert result.ok and result.rows[0][0] == 1 and str(result.rows[0][1]) == "2024-03-14"
    bad = sandbox.dry_run("SELECT nope FROM `sqlgami-dev-1109.no_such.table`")
    assert not bad.ok and bad.error
