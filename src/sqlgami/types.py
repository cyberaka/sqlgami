"""Coarse type families, used to spot comparisons between unlike things."""

from __future__ import annotations

import re
from functools import lru_cache

from sqlglot import exp

NUMERIC = "numeric"
TEXT = "text"
DATE = "date"
TIMESTAMP = "timestamp"
TIME = "time"
BOOLEAN = "boolean"
JSON = "json"
BINARY = "binary"
OTHER = "other"
UNKNOWN = "unknown"

TEMPORAL = {DATE, TIMESTAMP}

_T = exp.DataType.Type

_FAMILY_BY_TYPE: dict[exp.DataType.Type, str] = {}
for _t in exp.DataType.NUMERIC_TYPES:
    _FAMILY_BY_TYPE[_t] = NUMERIC
for _t in exp.DataType.TEXT_TYPES:
    _FAMILY_BY_TYPE[_t] = TEXT
for _t in exp.DataType.TEMPORAL_TYPES:
    _FAMILY_BY_TYPE[_t] = TIMESTAMP
for _name in ("DATE", "DATE32"):
    if hasattr(_T, _name):
        _FAMILY_BY_TYPE[getattr(_T, _name)] = DATE
for _name in ("TIME", "TIMETZ"):
    if hasattr(_T, _name):
        _FAMILY_BY_TYPE[getattr(_T, _name)] = TIME
for _name in ("BOOLEAN",):
    _FAMILY_BY_TYPE[getattr(_T, _name)] = BOOLEAN
for _name in ("JSON", "JSONB", "VARIANT", "OBJECT", "STRUCT", "MAP", "ARRAY", "LIST"):
    if hasattr(_T, _name):
        _FAMILY_BY_TYPE[getattr(_T, _name)] = JSON
for _name in ("BINARY", "VARBINARY", "BLOB"):
    if hasattr(_T, _name):
        _FAMILY_BY_TYPE[getattr(_T, _name)] = BINARY
for _name in ("UUID", "INTERVAL", "ENUM"):
    if hasattr(_T, _name):
        _FAMILY_BY_TYPE[getattr(_T, _name)] = OTHER
for _name in ("UNKNOWN", "NULL"):
    if hasattr(_T, _name):
        _FAMILY_BY_TYPE[getattr(_T, _name)] = UNKNOWN


def family_of_datatype(data_type: exp.DataType | None) -> str:
    if data_type is None:
        return UNKNOWN
    return _FAMILY_BY_TYPE.get(data_type.this, OTHER)


@lru_cache(maxsize=4096)
def parse_type(declared: str, dialect: str) -> exp.DataType | None:
    """Parse a declared column type, tolerating the loose types SQLite allows."""
    text = (declared or "").strip()
    if not text:
        return None
    try:
        return exp.DataType.build(text, dialect=dialect)
    except Exception:
        pass
    family = _family_from_name(text)
    fallback = {
        NUMERIC: "DOUBLE",
        TEXT: "TEXT",
        DATE: "DATE",
        TIMESTAMP: "TIMESTAMP",
        BOOLEAN: "BOOLEAN",
        JSON: "JSON",
        BINARY: "BLOB",
    }.get(family)
    if fallback is None:
        return None
    try:
        return exp.DataType.build(fallback, dialect=dialect)
    except Exception:
        return None


def _family_from_name(declared: str) -> str:
    upper = declared.upper()
    if "TIMESTAMP" in upper or "DATETIME" in upper:
        return TIMESTAMP
    if re.search(r"\bDATE\b", upper):
        return DATE
    if "BOOL" in upper:
        return BOOLEAN
    if "JSON" in upper:
        return JSON
    if "INT" in upper:
        return NUMERIC
    if any(token in upper for token in ("CHAR", "CLOB", "TEXT", "STRING")):
        return TEXT
    if "BLOB" in upper or "BYTE" in upper:
        return BINARY
    if any(token in upper for token in ("REAL", "FLOA", "DOUB", "NUM", "DEC", "MONEY")):
        return NUMERIC
    return UNKNOWN


@lru_cache(maxsize=4096)
def family_of_declared(declared: str, dialect: str) -> str:
    """The type family of a declared column type string."""
    text = (declared or "").strip()
    if not text:
        return UNKNOWN
    # SQLite keeps whatever the DDL said, so the name carries more than the affinity.
    by_name = _family_from_name(text)
    if dialect == "sqlite" and by_name != UNKNOWN:
        return by_name
    data_type = parse_type(text, dialect)
    if data_type is None:
        return by_name
    family = family_of_datatype(data_type)
    return by_name if family in (OTHER, UNKNOWN) and by_name != UNKNOWN else family


def sqlglot_type_name(declared: str, dialect: str) -> str:
    """A type string sqlglot's schema will accept, or ``UNKNOWN``."""
    data_type = parse_type(declared, dialect)
    if data_type is None:
        return "UNKNOWN"
    return data_type.sql(dialect=dialect)
