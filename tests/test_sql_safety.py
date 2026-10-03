import pytest

from cabinet.domains.sql_safety import inspect_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'UPDATE students; DROP TABLE x' AS text; -- harmless",
        "-- delete all rows\nSELECT 1",
        "SELECT $$; DROP TABLE x$$ AS text;",
        "SELECT 1 /* ; UPDATE x */;",
    ],
)
def test_literals_and_comments_do_not_trigger_guard(sql):
    assert inspect_sql(sql)[1] is None


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM users",
        "WITH x AS (DELETE FROM users RETURNING *) SELECT * FROM x",
        "SELECT 1; SELECT 2",
        "SELECT 1; -- second statement follows\nSELECT 2",
        "SELECT 'unterminated",
    ],
)
def test_writes_multiple_statements_and_invalid_literals_rejected(sql):
    assert inspect_sql(sql)[1]
