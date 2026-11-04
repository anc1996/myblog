"""Verify Chinese search primitives in a temporary table, not application search."""

from pathlib import Path
import sys

import jieba
import psycopg

from local_services import ROOT, credentials, require, verified_hosts


def search_tokens(text):
    """Index and query use the same tokenizer; PostgreSQL handles lexeme casing."""
    return " ".join(jieba.cut_for_search(text))


def main():
    require(
        Path.cwd() == ROOT
        and sys.executable.startswith("/root/anaconda3/envs/wagtailblog-test/"),
        "Use the authorized WSL cwd and Conda interpreter",
    )
    values, hosts = credentials(), verified_hosts()
    with psycopg.connect(
        host=hosts["postgres"], port=5432, user=values["POSTGRES_USER"],
        password=values["POSTGRES_PASSWORD"], dbname="myblog_test", connect_timeout=10,
    ) as connection:
        with connection.cursor() as cursor:
            # ON COMMIT DROP and session-local storage leave no application schema.
            cursor.execute(
                "CREATE TEMP TABLE myblog_search_probe "
                "(id integer PRIMARY KEY, body text NOT NULL, terms tsvector NOT NULL) "
                "ON COMMIT DROP"
            )
            for number, body in (
                (1, "中文全文搜索：数据库入门"),
                (2, "PostgreSQL 数据库与云计算 🎯"),
                (3, "图像处理教程"),
            ):
                cursor.execute(
                    "INSERT INTO myblog_search_probe VALUES (%s,%s,to_tsvector('simple',%s))",
                    (number, body, search_tokens(body)),
                )
            cursor.execute("CREATE INDEX ON myblog_search_probe USING GIN (terms)")
            for query, expected in (
                ("数据库", [1, 2]), ("中文 搜索", [1]),
                ("PostgreSQL 数据库", [2]), ("不存在的查询词", []),
            ):
                cursor.execute(
                    "SELECT id FROM myblog_search_probe "
                    "WHERE terms @@ plainto_tsquery('simple',%s) ORDER BY id",
                    (search_tokens(query),),
                )
                require([row[0] for row in cursor.fetchall()] == expected, "Chinese search mismatch")
            # A single character inside a compound word needs the explicit fallback.
            cursor.execute(
                "SELECT id FROM myblog_search_probe WHERE body LIKE %s ORDER BY id", ("%云%",),
            )
            require(cursor.fetchall() == [(2,)], "Single-character fallback mismatch")
    print("PostgreSQL temporary GIN index: Chinese, mixed-language, AND and single-character probes passed")
    print("Application publication, permissions, indexing workers and performance are not validated")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"PostgreSQL search probe failed ({type(exc).__name__}); credentials withheld", file=sys.stderr)
        sys.exit(1)
