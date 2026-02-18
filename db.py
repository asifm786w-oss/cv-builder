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
    conn.row_factory = sqlite3.Row  # dict-like rows
    return conn


def _pg_conn():
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    return psycopg2.connect(
        db_url,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def get_db_connection():
    """
    Legacy compatibility. Keep this name so old code doesn't break.
    """
    return _pg_conn() if is_postgres() else _sqlite_conn()


@contextmanager
def get_conn():
    """
    Unified connection context manager.
    Use:  with get_conn() as conn:
    """
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()


def _adapt_sql(sql: str) -> str:
    """
    Standardize your whole app on writing SQL with %s placeholders.
    For SQLite, we auto-convert %s -> ?
    """
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
    """
    Executes a write query and commits. Returns rowcount.
    """
    sql = _adapt_sql(sql)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return int(getattr(cur, "rowcount", 0) or 0)


def scalar(sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
    """
    Returns the first column of the first row, or default.
    """
    sql = _adapt_sql(sql)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        if not row:
            return default
        # sqlite Row + pg RealDict both support indexing by 0 here
        try:
            return row[0]
        except Exception:
            # fallback if row is dict-like
            return list(row.values())[0] if hasattr(row, "values") else default


@contextmanager
def tx():
    """
    Transaction helper.
    Use:
        with tx() as conn:
            conn.cursor().execute(...)
    Commits on success, rollbacks on exception.
    """
    with get_conn() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
