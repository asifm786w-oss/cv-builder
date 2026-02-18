# db.py
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Mapping, Optional, Sequence

import psycopg2
import psycopg2.extras

DB_PATH = "users.db"


def is_postgres() -> bool:
    return bool((os.getenv("DATABASE_URL") or "").strip())


def _sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _pg_conn():
    db_url = os.getenv("DATABASE_URL", "").strip()
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    return psycopg2.connect(
        db_url,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def get_db_connection():
    """
    Backwards-compatible name. Do not remove.
    Returns dict-like rows in BOTH sqlite and postgres.
    """
    return _pg_conn() if is_postgres() else _sqlite_conn()


@contextmanager
def get_conn():
    """
    Preferred API. Always use:
        with get_conn() as conn:
            cur = conn.cursor()
    """
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()


def _adapt_sql(sql: str) -> str:
    # Write SQL using %s everywhere.
    return sql if is_postgres() else sql.replace("%s", "?")


def fetchone(sql: str, params: Sequence[Any] = ()) -> Optional[Mapping[str, Any]]:
    sql = _adapt_sql(sql)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def fetchall(sql: str, params: Sequence[Any] = ()) -> list[Mapping[str, Any]]:
    sql = _adapt_sql(sql)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall() or []
        return [dict(r) for r in rows]


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    sql = _adapt_sql(sql)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return int(getattr(cur, "rowcount", 0) or 0)
