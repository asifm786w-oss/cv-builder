# db.py
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

DB_PATH = "users.db"


def is_postgres() -> bool:
    return bool((os.getenv("DATABASE_URL") or "").strip())


def _sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # ✅ dict-like rows
    return conn


def _pg_conn():
    import psycopg2
    import psycopg2.extras

    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    # ✅ dict rows always
    return psycopg2.connect(
        db_url,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _adapt_sql(sql: str) -> str:
    """
    Standardize on ONE placeholder style in your app:
    - Write queries using %s
    - If sqlite, convert %s -> ?
    """
    if not is_postgres():
        return sql.replace("%s", "?")
    return sql


@contextmanager
def get_conn():
    """
    Unified connection context manager.
    - sqlite: autocommit by commit() call
    - postgres: same
    """
    conn = _pg_conn() if is_postgres() else _sqlite_conn()
    try:
        yield conn
    finally:
        conn.close()


def fetchone(sql: str, params: Sequence[Any] = ()) -> Optional[Mapping[str, Any]]:
    sql2 = _adapt_sql(sql)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql2, params)
        row = cur.fetchone()
        if not row:
            return None

        # sqlite Row -> dict
        if not is_postgres():
            return dict(row)

        # postgres RealDictCursor already dict
        return row


def fetchall(sql: str, params: Sequence[Any] = ()) -> list[Mapping[str, Any]]:
    sql2 = _adapt_sql(sql)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql2, params)
        rows = cur.fetchall() or []

        if not is_postgres():
            return [dict(r) for r in rows]

        return rows


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """
    Returns rowcount.
    """
    sql2 = _adapt_sql(sql)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql2, params)
        conn.commit()
        return getattr(cur, "rowcount", 0)
