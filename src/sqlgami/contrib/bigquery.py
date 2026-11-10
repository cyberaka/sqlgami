"""The real BigQuery service, OFFLINE only (ADR 0003, B2): dry runs, native reference runs, and
loading fixtures into the sandbox to prove the DuckDB translation. Never used by ``score()``.

The project is taken from ``SQLGAMI_BQ_PROJECT`` (or passed in), must start with ``sqlgami-``,
and the only project this module will ever talk to is the sandbox ``sqlgami-dev-1109`` (billing
disabled). Any other value raises before a client is built. Authentication is application-default
credentials; no key files are read. Datasets are created with a ``bench_`` prefix, given a default
table expiry, and recorded in ``bench/.runs/bigquery/datasets.jsonl`` so they can be cleaned up.

``pip install sqlgami[bigquery]``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..errors import ConfigurationError, SqlgamiError

SANDBOX_PROJECTS = ("sqlgami-dev-1109",)
PROJECT_PREFIX = "sqlgami-"
DATASET_PREFIX = "bench_"
DEFAULT_EXPIRY_DAYS = 60
LEDGER = Path("bench/.runs/bigquery/datasets.jsonl")


class BigQueryError(SqlgamiError):
    pass


def sandbox_project(project: str | None = None) -> str:
    """The one project allowed, or :class:`ConfigurationError`. Checked before any client exists."""
    value = (project or os.environ.get("SQLGAMI_BQ_PROJECT") or "").strip()
    if not value:
        raise ConfigurationError("no BigQuery project: set SQLGAMI_BQ_PROJECT to the sqlgami sandbox project")
    if not value.startswith(PROJECT_PREFIX):
        raise ConfigurationError(f"BigQuery project {value!r} is not a sqlgami- project; refusing")
    if value not in SANDBOX_PROJECTS:
        raise ConfigurationError(f"BigQuery project {value!r} is not the sqlgami sandbox ({', '.join(SANDBOX_PROJECTS)}); refusing")
    return value


def dataset_name(name: str) -> str:
    if not name.startswith(DATASET_PREFIX):
        raise ConfigurationError(f"dataset {name!r} must start with {DATASET_PREFIX!r}")
    return name


@dataclass
class DryRun:
    ok: bool
    bytes_processed: int | None = None
    error: str | None = None
    columns: list[str] = field(default_factory=list)


@dataclass
class NativeResult:
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    error: str | None = None
    bytes_processed: int | None = None
    job_id: str | None = None


class BigQuerySandbox:
    """A client bound to the sandbox project. Every method is an explicit, offline action."""

    def __init__(self, project: str | None = None, *, location: str = "US", ledger: Path | None = None) -> None:
        self.project = sandbox_project(project)
        try:
            from google.cloud import bigquery  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ConfigurationError("BigQuery needs google-cloud-bigquery: pip install sqlgami[bigquery]") from exc
        self._bq = bigquery
        self.client = bigquery.Client(project=self.project)  # application-default credentials only
        if self.client.project != self.project:
            raise ConfigurationError(f"the BigQuery client resolved project {self.client.project!r}, not {self.project!r}; refusing")
        self.location = location
        self.ledger = ledger or LEDGER

    # ------------------------------------------------------------- queries

    def dry_run(self, sql: str, params: dict[str, Any] | None = None) -> DryRun:
        """Validate and price a statement without running it."""
        config = self._bq.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=self._parameters(params))
        try:
            job = self.client.query(sql, job_config=config, location=self.location)
        except Exception as exc:  # the service's error text is the finding
            return DryRun(ok=False, error=_first_lines(str(exc)))
        columns = [f.name for f in (job.schema or [])]
        return DryRun(ok=True, bytes_processed=job.total_bytes_processed, columns=columns)

    def run(self, sql: str, params: dict[str, Any] | None = None, *, max_rows: int = 10_000, max_bytes: int = 1_000_000_000) -> NativeResult:
        """Run a statement natively; refuses anything that would read more than ``max_bytes``."""
        priced = self.dry_run(sql, params)
        if not priced.ok:
            return NativeResult(ok=False, error=priced.error)
        if priced.bytes_processed is not None and priced.bytes_processed > max_bytes:
            return NativeResult(ok=False, error=f"would process {priced.bytes_processed} bytes, over the {max_bytes} limit")
        config = self._bq.QueryJobConfig(use_query_cache=False, query_parameters=self._parameters(params), maximum_bytes_billed=max_bytes)
        try:
            job = self.client.query(sql, job_config=config, location=self.location)
            rows = list(job.result(max_results=max_rows))
        except Exception as exc:
            return NativeResult(ok=False, error=_first_lines(str(exc)))
        columns = [f.name for f in job.result().schema]
        return NativeResult(ok=True, columns=columns, rows=[tuple(r.values()) for r in rows], bytes_processed=job.total_bytes_processed, job_id=job.job_id)

    def _parameters(self, params: dict[str, Any] | None) -> list[Any]:
        found = []
        for name, value in (params or {}).items():
            if isinstance(value, bool):
                kind = "BOOL"
            elif isinstance(value, int):
                kind = "INT64"
            elif isinstance(value, float):
                kind = "FLOAT64"
            elif isinstance(value, dt.datetime):
                kind = "TIMESTAMP"
            elif isinstance(value, dt.date):
                kind = "DATE"
            else:
                kind = "STRING"
            found.append(self._bq.ScalarQueryParameter(name, kind, value))
        return found

    # ------------------------------------------------------------- datasets

    def ensure_dataset(self, name: str, *, expiry_days: int = DEFAULT_EXPIRY_DAYS, note: str = "") -> str:
        """Create ``bench_<name>`` (idempotent) with a default table expiry, and record it."""
        name = dataset_name(name)
        ref = self._bq.Dataset(f"{self.project}.{name}")
        ref.location = self.location
        ref.default_table_expiration_ms = expiry_days * 24 * 3600 * 1000
        self.client.create_dataset(ref, exists_ok=True)
        self._record({"action": "ensure_dataset", "project": self.project, "dataset": name, "expiry_days": expiry_days, "note": note, "at": _now()})
        return name

    def load_table(self, dataset: str, table: str, columns: Sequence[tuple[str, str]], rows: Iterable[Sequence[Any]], *, replace: bool = True) -> int:
        """Load rows into ``dataset.table`` with an explicit schema (BigQuery type names). Load
        jobs are free in the sandbox. Returns the row count."""
        dataset = dataset_name(dataset)
        schema = [self._bq.SchemaField(name, kind) for name, kind in columns]
        table_id = f"{self.project}.{dataset}.{table}"
        config = self._bq.LoadJobConfig(schema=schema, write_disposition="WRITE_TRUNCATE" if replace else "WRITE_APPEND", source_format="NEWLINE_DELIMITED_JSON")
        payload = [dict(zip([c for c, _ in columns], [_json_value(v) for v in row])) for row in rows]
        job = self.client.load_table_from_json(payload, table_id, job_config=config, location=self.location)
        job.result()
        self._record({"action": "load_table", "project": self.project, "dataset": dataset, "table": table, "rows": len(payload), "at": _now()})
        return len(payload)

    def delete_dataset(self, name: str) -> None:
        name = dataset_name(name)
        self.client.delete_dataset(f"{self.project}.{name}", delete_contents=True, not_found_ok=True)
        self._record({"action": "delete_dataset", "project": self.project, "dataset": name, "at": _now()})

    def list_bench_datasets(self) -> list[str]:
        return [d.dataset_id for d in self.client.list_datasets(project=self.project) if d.dataset_id.startswith(DATASET_PREFIX)]

    def _record(self, entry: dict[str, Any]) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


def _json_value(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    try:
        from decimal import Decimal

        if isinstance(value, Decimal):
            return str(value)
    except ImportError:  # pragma: no cover
        pass
    return value


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _first_lines(text: str, n: int = 3) -> str:
    return "\n".join(text.strip().splitlines()[:n])
