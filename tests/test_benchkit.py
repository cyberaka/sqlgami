"""The bench tooling, exercised on a miniature suite kept beside the tests."""

from __future__ import annotations

import datetime as dt
import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("yaml")

from benchkit import build, compare, evaluate, validate  # noqa: E402
from benchkit.suite import SuiteError, load_suite  # noqa: E402

MINI = Path(__file__).parent / "minisuite"


@pytest.fixture(scope="module")
def data_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("benchdata")
    original = build.DATA_DIR
    build.DATA_DIR = root
    yield root
    build.DATA_DIR = original


@pytest.fixture(scope="module")
def suite(data_root):
    return load_suite(MINI)


def test_suite_loads(suite):
    assert suite.name == "minisuite"
    assert [s.label for s in suite.seeds] == ["clean-1", "clean-2", "clean-3", "nasty-42"]
    assert len(suite.questions) == 4 and len(suite.bugs) == 12 and len(suite.equivalents) == 4
    assert suite.tenant_column == "tenant_id" and suite.soft_delete_column == "deleted_at"
    assert suite.question("minisuite-q004").answerable is False
    with pytest.raises(SuiteError):
        load_suite(MINI / "schema")


def test_generation_is_repeatable(suite, tmp_path):
    first = build.generate(suite, suite.seeds[0], out=tmp_path / "a", force=True)
    second = build.generate(suite, suite.seeds[0], out=tmp_path / "b", force=True)
    other = build.generate(suite, suite.seeds[1], out=tmp_path / "c", force=True)
    assert build.digest(first) == build.digest(second) != build.digest(other)
    assert build.row_counts(first)["shop.customers"] == 20


def test_split_statements_respects_strings_and_comments():
    sql = "-- a; comment\nCREATE TABLE t (a TEXT DEFAULT ';'); /* x; y */ INSERT INTO t VALUES ('it''s; fine');\n"
    assert build.split_statements(sql) == [
        "CREATE TABLE t (a TEXT DEFAULT ';')",
        "INSERT INTO t VALUES ('it''s; fine')",
    ]


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_databases_build_and_hold_the_same_data(suite, engine):
    seed = suite.seeds[-1]
    conn = build.connect(suite, engine, seed)
    try:
        table = "shop_customers" if engine == "sqlite" else "shop.customers"
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 20
        vip = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE is_vip").fetchone()[0]
        assert vip == 4
        deleted = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE deleted_at IS NOT NULL").fetchone()[0]
        assert deleted == 1
        with pytest.raises(Exception):
            conn.execute(f"DELETE FROM {table}")
    finally:
        conn.close()


def test_values_compare_across_engines():
    same = compare.same_rows
    assert same([(1, "2026-01-05T10:00:00+00:00", 1)], [(1.0, dt.datetime(2026, 1, 5, 10, tzinfo=dt.timezone.utc), True)])[0]
    assert same([(Decimal("12.50"),)], [(12.5,)])[0]
    assert same([(0.1 + 0.2,)], [(0.3,)])[0]
    assert same([("2026-01-05",)], [(dt.date(2026, 1, 5),)])[0]
    assert same([('{"b":1,"a":2}',)], [({"a": 2, "b": 1},)])[0]
    assert same([(1,), (2,), (None,)], [(None,), (2,), (1,)])[0]
    assert not same([(1,), (2,)], [(2,), (1,)], ordered=True)[0]
    assert not same([(1,), (1,), (2,)], [(1,), (2,), (2,)])[0]  # duplicates count
    assert not same([(1,)], [(1,), (1,)])[0]
    assert not same([("US",)], [("us",)])[0]
    assert not same([(1, 2)], [(1,)])[0]
    assert same([], [])[0]


def test_the_minisuite_validates(suite):
    result = validate.validate(suite)
    assert result.ok, result.summary()
    assert result.counts == {"questions": 4, "bugs": 12, "equivalents": 4}


def _broken_copy(tmp_path, mutate) -> Path:
    target = tmp_path / "broken"
    shutil.copytree(MINI, target)
    mutate(target)
    return target


def _rewrite(path: Path, change) -> None:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    path.write_text("".join(json.dumps(change(r) or r) + "\n" for r in rows))


def test_validator_catches_a_bug_that_is_not_a_bug(suite, tmp_path):
    def mutate(root):
        def change(row):
            if row["id"] == "minisuite-b001-duckdb":
                row["sql"] = row["sql"].replace("AND c.deleted_at IS NULL", "AND c.deleted_at IS NULL AND o.status = 'paid'")
            return row

        _rewrite(root / "bugs.jsonl", change)

    result = validate.validate(load_suite(_broken_copy(tmp_path, mutate)), check_determinism=False)
    assert not result.ok
    assert any("not a bug" in p.message and p.where == "minisuite-b001-duckdb" for p in result.problems)


def test_validator_catches_wrong_gold_and_false_equivalents(suite, tmp_path):
    def mutate(root):
        def gold(row):
            if row["id"] == "minisuite-q002":
                row["gold_sql"]["sqlite"] = row["gold_sql"]["sqlite"].replace("'US'", "'DE'")
            return row

        def equivalent(row):
            if row["id"] == "minisuite-e001-sqlite":
                row["sql"] = row["sql"].replace("WHERE status = 'paid' ", "")
            return row

        def bug(row):
            if row["id"] == "minisuite-b004-duckdb":
                row["expected_outcome"] = "wrong_on_nasty_only"
            return row

        _rewrite(root / "questions.jsonl", gold)
        _rewrite(root / "equivalents.jsonl", equivalent)
        _rewrite(root / "bugs.jsonl", bug)

    result = validate.validate(load_suite(_broken_copy(tmp_path, mutate)), check_determinism=False)
    messages = "\n".join(str(p) for p in result.problems)
    assert "gold differs between sqlite and duckdb" in messages
    assert "equivalent does not match gold" in messages
    assert "minisuite-b004-duckdb" in messages


def test_metrics():
    assert evaluate.auroc([0.9, 0.8, 0.2, 0.1], [True, True, False, False]) == 1.0
    assert evaluate.auroc([0.1, 0.2, 0.8, 0.9], [True, True, False, False]) == 0.0
    assert evaluate.auroc([0.5, 0.5], [True, False]) == 0.5
    assert evaluate.auroc([0.5, 0.6], [True, True]) is None
    assert evaluate.expected_calibration_error([1.0, 1.0, 0.0, 0.0], [True, True, False, False]) == 0.0
    assert evaluate.expected_calibration_error([0.95, 0.95], [False, False]) == pytest.approx(0.95)


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_evaluation_separates_right_from_wrong(suite, engine):
    items, result = evaluate.evaluate(suite, engine, "sample")
    by_id = {i.id: i for i in items}
    assert result["items"] == 3 + 2 + 6
    assert result["correct"] == 5 and result["wrong"] == 6
    assert all(i.correct for i in items if i.kind in ("gold", "equivalent"))
    assert not any(i.correct for i in items if i.kind == "bug")
    assert result["auroc"] is not None and result["auroc"] >= 0.8, evaluate.format_metrics(result)
    assert result["correct_flagged_share"] == 0, [(i.id, i.findings) for i in items if i.correct and i.score <= 0.4]
    for bug, code in (("b003", "missing_tenant_filter"), ("b004", "unknown_column"), ("b005", "filter_value"), ("b006", "count_grain")):
        item = by_id[f"minisuite-{bug}-{engine}"]
        assert code in item.findings and item.score <= 0.4, (item.id, item.findings, item.score)
    assert by_id[f"minisuite-b004-{engine}"].runs is False
    text = evaluate.format_metrics(result)
    assert "AUROC" in text and "by bug class" in text


def test_label_candidates(suite, tmp_path):
    gold = suite.question("minisuite-q002").gold("duckdb")
    rows = [
        {"question_id": "minisuite-q002", "engine": "duckdb", "sql": gold, "generator": "m", "sample": 0},
        {"question_id": "minisuite-q002", "engine": "duckdb", "sql": gold.replace("'US'", "'DE'"), "generator": "m", "sample": 1},
        {"question_id": "minisuite-q002", "engine": "duckdb", "sql": "SELECT nope FROM shop.customers", "generator": "m", "sample": 2},
        {"question_id": "minisuite-q004", "engine": "duckdb", "sql": "SELECT 1", "generator": "m", "sample": 0},
        {"question_id": "no-such-question", "engine": "duckdb", "sql": "SELECT 1", "generator": "m", "sample": 0},
    ]
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    (run_dir / "candidates.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = evaluate.label_run(suite, run_dir)
    labels = [json.loads(line) for line in out.read_text().splitlines()]
    assert [l["correct"] for l in labels] == [True, False, False, None, None]
    assert labels[0]["per_seed"] == {seed.label: True for seed in suite.seeds}
    assert labels[2]["runs"] is False and labels[1]["runs"] is True
    assert "no gold" in labels[3]["note"] and "unknown question" in labels[4]["note"]


def test_audit_finds_gold_that_strong_generators_agree_against(suite, tmp_path):
    from benchkit import audit as audit_mod

    gold = suite.question("minisuite-q002").gold("duckdb")
    other = gold.replace("'US'", "'DE'")  # two strong generators agree on this, against gold
    rows = [
        {"question_id": "minisuite-q002", "engine": "duckdb", "sql": other, "generator": "claude-opus", "sample": 0},
        {"question_id": "minisuite-q002", "engine": "duckdb", "sql": other, "generator": "claude-haiku", "sample": 0},
        {"question_id": "minisuite-q002", "engine": "duckdb", "sql": other, "generator": "hf:small", "sample": 0},
        {"question_id": "minisuite-q002", "engine": "duckdb", "sql": gold, "generator": "claude-sonnet", "sample": 0},
        {"question_id": "minisuite-q003", "engine": "duckdb", "sql": suite.question("minisuite-q003").gold("duckdb"), "generator": "claude-opus", "sample": 0},
        {"question_id": "minisuite-q003", "engine": "duckdb", "sql": suite.question("minisuite-q003").gold("duckdb"), "generator": "claude-haiku", "sample": 0},
    ]
    path = tmp_path / "candidates.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    suspects = audit_mod.audit(suite, [path], ["duckdb"])
    assert [s.question_id for s in suspects] == ["minisuite-q002"]
    assert sorted(suspects[0].generators) == ["claude-haiku", "claude-opus"]
    assert suspects[0].others_agreeing == ["hf:small"]
    assert suspects[0].seeds_differing  # on at least one seed
    # One strong generator alone is not an audit finding.
    assert audit_mod.audit(suite, [path], ["duckdb"], strong=("opus",)) == []


def test_labeller_matches_columns_by_name_before_position():
    from benchkit import compare

    gold = compare.Outcome(True, [(1, "a"), (2, "b")], ["id", "name"])
    reordered = compare.Outcome(True, [("a", 1), ("b", 2)], ["name", "id"])
    assert compare.same_outcome(gold, reordered)[0]
    swapped_values = compare.Outcome(True, [("a", 1), ("b", 2)], ["id", "name"])
    assert not compare.same_outcome(gold, swapped_values)[0]
    unnamed = compare.Outcome(True, [("a", 1), ("b", 2)], ["x", "y"])
    assert not compare.same_outcome(gold, unnamed)[0]  # different names: positional, so it differs
    assert compare.same_rows([(True,), (False,)], [(1,), (0,)])[0]  # bool and 0/1 are equal


def test_validator_warns_on_an_empty_suite_and_names_skipped_engines(tmp_path, monkeypatch):
    monkeypatch.delenv("SQLGAMI_BENCH_PG_DSN", raising=False)
    monkeypatch.delenv("SQLGAMI_TEST_PG_DSN", raising=False)

    def mutate(root):
        for name in ("questions", "bugs", "equivalents"):
            (root / f"{name}.jsonl").write_text("")
        config = (root / "suite.yaml").read_text().replace("engines: [sqlite, duckdb]", "engines: [sqlite, duckdb, postgres]")
        (root / "suite.yaml").write_text(config)

    broken = load_suite(_broken_copy(tmp_path, mutate))
    lenient = validate.validate(broken, check_determinism=False)
    assert lenient.ok
    text = lenient.summary()
    assert "the suite has no questions" in text and "skipped: postgres" in text
    strict = validate.validate(broken, check_determinism=False, strict=True)
    assert not strict.ok


def test_accepted_alternative_answers(suite, tmp_path):
    def mutate(root):
        def change(row):
            if row["id"] == "minisuite-q002":
                row["answerable"] = "ambiguous"
                row["gold_alternatives"] = [
                    {"note": "reading US as North America, which here includes DE by mistake",
                     "sql": {"duckdb": "SELECT COUNT(*) FROM shop.customers WHERE country IN ('US', 'DE') AND deleted_at IS NULL",
                             "sqlite": "SELECT COUNT(*) FROM shop_customers WHERE country IN ('US', 'DE') AND deleted_at IS NULL"}}
                ]
            return row

        _rewrite(root / "questions.jsonl", change)
        (root / "bugs.jsonl").write_text("")

    alt_suite = load_suite(_broken_copy(tmp_path, mutate))
    assert validate.validate(alt_suite, check_determinism=False).ok
    rows = [
        {"question_id": "minisuite-q002", "engine": "duckdb", "sql": "SELECT COUNT(*) FROM shop.customers WHERE country = 'US' AND deleted_at IS NULL"},
        {"question_id": "minisuite-q002", "engine": "duckdb", "sql": "SELECT COUNT(id) FROM shop.live_customers_v WHERE country = 'US' OR country = 'DE'"},
        {"question_id": "minisuite-q002", "engine": "duckdb", "sql": "SELECT COUNT(*) FROM shop.customers WHERE country = 'DE'"},
    ]
    run_dir = tmp_path / "run-alt"
    run_dir.mkdir()
    (run_dir / "candidates.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    labels = [json.loads(line) for line in evaluate.label_run(alt_suite, run_dir).read_text().splitlines()]
    assert [l["correct"] for l in labels] == [True, True, False]
    assert labels[0]["matched"] == "gold" and labels[1]["matched"].startswith("alternative 1") and labels[2]["matched"] is None


def test_complexity_statistics(suite):
    stats = validate.complexity_stats(suite)
    assert set(stats) == {"simple", "moderate"}
    assert stats["moderate"]["questions"] == 1 and stats["moderate"]["joins_max"] == 1
    assert stats["simple"]["production_grade"] == 0
    shape = validate.sql_shape(
        "WITH a AS (SELECT 1 AS x), b AS (SELECT x FROM a) SELECT ROW_NUMBER() OVER (ORDER BY b.x) "
        "FROM b JOIN a ON a.x = b.x WHERE b.x IN (SELECT x FROM a)",
        "duckdb",
    )
    assert (shape["joins"], shape["ctes"], shape["windows"]) == (1, 2, 1) and shape["depth"] >= 2
    assert "gold SQL complexity by difficulty" in validate.validate(suite, check_determinism=False).summary()
