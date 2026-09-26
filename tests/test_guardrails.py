import pytest

from app.guardrails import check_cost, check_query

PROJECT = "test-project"
PII = ["customers.customers.email", "customers.customers.phone"]


def check(sql, datasets=("marketing",)):
    return check_query(sql, list(datasets), PROJECT, PII)


APPROVED = [
    "SELECT campaign_id, SUM(spend) AS spend FROM marketing.campaigns GROUP BY campaign_id",
    "SELECT * FROM marketing.campaigns",
    "SELECT * FROM `test-project.marketing.campaigns`",
    "SELECT * FROM `test-project`.marketing.campaigns",
    "SELECT c.name, SUM(v.conversions) FROM marketing.campaigns c "
    "JOIN marketing.conversions v ON c.id = v.campaign_id GROUP BY c.name",
    "WITH q3 AS (SELECT * FROM marketing.campaigns WHERE quarter = 3) "
    "SELECT name, spend FROM q3 ORDER BY spend DESC",
    "SELECT name, RANK() OVER (ORDER BY spend DESC) AS r FROM marketing.campaigns",
    "SELECT id FROM marketing.campaigns UNION ALL SELECT id FROM marketing.archive",
    "SELECT * FROM marketing.events_* WHERE _TABLE_SUFFIX > '20260101'",
    "SELECT table_name FROM marketing.INFORMATION_SCHEMA.TABLES",
    "SELECT x FROM UNNEST([1, 2, 3]) AS x",
    "SELECT 1",
    "SELECT * FROM marketing.campaigns -- trailing comment",
]


@pytest.mark.parametrize("sql", APPROVED)
def test_approved(sql):
    decision = check(sql)
    assert decision.approved, decision.reason


REJECTED = {
    "drop": ("DROP TABLE marketing.campaigns", "SELECT"),
    "delete": ("DELETE FROM marketing.campaigns WHERE true", "SELECT"),
    "insert": ("INSERT INTO marketing.campaigns (id) VALUES (1)", "SELECT"),
    "update": ("UPDATE marketing.campaigns SET spend = 0 WHERE true", "SELECT"),
    "merge": (
        "MERGE marketing.a t USING marketing.b s ON t.id = s.id "
        "WHEN MATCHED THEN DELETE",
        "SELECT",
    ),
    "create": ("CREATE TABLE marketing.x AS SELECT 1", "SELECT"),
    "truncate": ("TRUNCATE TABLE marketing.campaigns", "SELECT"),
    "stacked": ("SELECT 1; DROP TABLE marketing.campaigns", "exactly one"),
    "comment trick": (
        "SELECT 1 /* harmless */; DROP TABLE marketing.campaigns --",
        "exactly one",
    ),
    "execute immediate": ("EXECUTE IMMEDIATE 'DROP TABLE marketing.campaigns'", "SELECT"),
    "call": ("CALL marketing.proc()", "SELECT"),
    "declare": ("DECLARE x INT64", "SELECT"),
    "empty": ("", "exactly one"),
    "garbage": ("SELEC garbage FROM", "parse"),
    "other project": ("SELECT * FROM `other-proj.marketing.campaigns`", "outside project"),
    "unqualified": ("SELECT * FROM campaigns", "qualified"),
    "disallowed dataset": ("SELECT * FROM finance.revenue", "not allowed"),
    "via join": (
        "SELECT * FROM marketing.campaigns c JOIN finance.revenue r ON c.id = r.id",
        "not allowed",
    ),
    "via subquery": (
        "SELECT * FROM marketing.campaigns WHERE id IN (SELECT id FROM finance.revenue)",
        "not allowed",
    ),
    "via cte": ("WITH r AS (SELECT * FROM finance.revenue) SELECT * FROM r", "not allowed"),
    "via union": (
        "SELECT id FROM marketing.campaigns UNION ALL SELECT id FROM finance.revenue",
        "not allowed",
    ),
    "info schema other dataset": (
        "SELECT * FROM finance.INFORMATION_SCHEMA.COLUMNS",
        "not allowed",
    ),
    "info schema region": (
        "SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS",
        "not allowed",
    ),
    "external query": (
        "SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT * FROM users')",
        "Table functions",
    ),
    "ml predict": (
        "SELECT * FROM ML.PREDICT(MODEL marketing.m, TABLE marketing.t)",
        "Table functions",
    ),
}


@pytest.mark.parametrize("sql,reason", REJECTED.values(), ids=REJECTED.keys())
def test_rejected(sql, reason):
    decision = check(sql)
    assert not decision.approved
    assert reason.lower() in decision.reason.lower()


PII_REJECTED = {
    "direct column": "SELECT email FROM customers.customers",
    "qualified column": "SELECT c.phone FROM customers.customers c",
    "aliased away": "SELECT email AS e FROM customers.customers",
    "via cte": "WITH x AS (SELECT email AS e FROM customers.customers) SELECT e FROM x",
    "in where": "SELECT id FROM customers.customers WHERE email LIKE '%@corp.com'",
    "uppercase": "SELECT EMAIL FROM customers.customers",
    "star": "SELECT * FROM customers.customers",
    "table star": "SELECT c.* FROM customers.customers c",
    "star in subquery": "SELECT id FROM (SELECT * FROM customers.customers)",
    "whole row by alias": "SELECT TO_JSON_STRING(c) FROM customers.customers c",
    "whole row by name": "SELECT customers FROM customers.customers",
}


@pytest.mark.parametrize("sql", PII_REJECTED.values(), ids=PII_REJECTED.keys())
def test_pii_rejected(sql):
    decision = check(sql, datasets=("marketing", "customers"))
    assert not decision.approved
    assert "pii" in decision.reason.lower()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM customers.customers",
        "SELECT country, COUNT(*) AS n FROM customers.customers GROUP BY country",
    ],
)
def test_count_star_on_pii_table_is_allowed(sql):
    decision = check(sql, datasets=("customers",))
    assert decision.approved, decision.reason


def test_count_star_does_not_hide_select_star():
    decision = check("SELECT *, COUNT(*) OVER () FROM customers.customers", ("customers",))
    assert not decision.approved


def test_non_pii_columns_of_pii_table_are_allowed():
    decision = check(
        "SELECT id, signup_date FROM customers.customers", datasets=("customers",)
    )
    assert decision.approved, decision.reason
    assert decision.tables == ["customers.customers"]


def test_approved_decision_lists_tables():
    decision = check(
        "SELECT * FROM marketing.campaigns c JOIN marketing.conversions v USING (id)"
    )
    assert decision.tables == ["marketing.campaigns", "marketing.conversions"]


class FakeEstimator:
    def __init__(self, result):
        self.result = result

    def estimate_bytes(self, sql):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_cost_under_limit():
    assert check_cost("SELECT 1", FakeEstimator(100), max_bytes=1000).approved


def test_cost_at_limit():
    assert check_cost("SELECT 1", FakeEstimator(1000), max_bytes=1000).approved


def test_cost_over_limit():
    decision = check_cost("SELECT 1", FakeEstimator(1001), max_bytes=1000)
    assert not decision.approved
    assert "limit" in decision.reason


def test_cost_estimator_error_rejects():
    decision = check_cost("SELECT 1", FakeEstimator(RuntimeError("boom")), max_bytes=1000)
    assert not decision.approved
    assert "boom" in decision.reason