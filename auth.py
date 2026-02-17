import os
import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Any

DB_PATH = "users.db"
RESET_TOKEN_EXPIRY_HOURS = 2

# Referral defaults (can be overridden from App.py by setting globals)
REFERRAL_CAP = 10
BONUS_PER_REFERRAL_CV = 5
BONUS_PER_REFERRAL_AI = 5

STARTER_CV = 5
STARTER_AI = 5


# -------------------------
# DB helpers (SQLite local / Postgres prod)
# -------------------------
def _is_postgres() -> bool:
    return bool((os.getenv("DATABASE_URL") or "").strip())


def get_conn():
    """
    Local dev: SQLite (users.db)
    Production (Railway): Postgres via DATABASE_URL
    """
    db_url = (os.getenv("DATABASE_URL") or "").strip()

    # Local dev (SQLite)
    if not db_url:
        conn = sqlite3.connect(DB_PATH)
        # helpful: return tuples like normal; keep default row type
        return conn

    # Production (Postgres)
    import psycopg2

    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    return psycopg2.connect(db_url)


def db_execute(cur, sql: str, params: tuple = ()):
    """
    Execute SQL using SQLite-style '?' placeholders in BOTH SQLite and Postgres.
    If Postgres is used, convert '?' -> '%s'.
    """
    if _is_postgres() and "?" in sql:
        sql = sql.replace("?", "%s")
    return cur.execute(sql, params)


def _fetchone(cur):
    return cur.fetchone()


def _fetchall(cur):
    return cur.fetchall()


def _col_exists(cur, table: str, col: str) -> bool:
    if _is_postgres():
        db_execute(
            cur,
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = ? AND column_name = ?
            """,
            (table, col),
        )
        return bool(_fetchone(cur))
    else:
        db_execute(cur, f"PRAGMA table_info({table})")
        cols = {r[1] for r in _fetchall(cur)}
        return col in cols


# -------------------------
# Schema init
# -------------------------
def init_db() -> None:
    """
    Initialise DB schema.
    Safe to call on every run.
    SQLite: creates + migrations.
    Postgres: creates tables if missing + lightweight type fixes.
    """
    conn = get_conn()
    cur = conn.cursor()

    if _is_postgres():
        # Users table (correct prod types)
        db_execute(
            cur,
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                full_name TEXT,
                plan TEXT NOT NULL DEFAULT 'free',
                is_admin INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'user',
                is_banned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                upload_parses INTEGER NOT NULL DEFAULT 0,
                summary_uses INTEGER NOT NULL DEFAULT 0,
                cover_uses INTEGER NOT NULL DEFAULT 0,
                bullets_uses INTEGER NOT NULL DEFAULT 0,
                cv_generations INTEGER NOT NULL DEFAULT 0,
                reset_token TEXT,
                reset_token_created_at TEXT,
                referral_code TEXT UNIQUE,
                referred_by TEXT,
                referrals_count INTEGER NOT NULL DEFAULT 0,
                accepted_policies BOOLEAN NOT NULL DEFAULT FALSE,
                accepted_policies_at TIMESTAMPTZ,
                job_summary_uses INTEGER NOT NULL DEFAULT 0
            );
            """,
        )

        # Credit ledger tables (needed for usage / referrals / starter)
        db_execute(
            cur,
            """
            CREATE TABLE IF NOT EXISTS credit_grants (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                cv_amount INTEGER NOT NULL DEFAULT 0,
                ai_amount INTEGER NOT NULL DEFAULT 0,
                expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """,
        )
        db_execute(
            cur,
            """
            CREATE TABLE IF NOT EXISTS credit_spends (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                cv_amount INTEGER NOT NULL DEFAULT 0,
                ai_amount INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """,
        )

        # Ensure unique(source) for idempotency
        try:
            db_execute(cur, "ALTER TABLE credit_grants ADD CONSTRAINT credit_grants_source_key UNIQUE (source)")
        except Exception:
            # already exists
            conn.rollback()
        else:
            conn.commit()

        conn.commit()

        # Lightweight migrations (don’t crash if already correct)
        # accepted_policies: if legacy int/text, convert to boolean
        try:
            db_execute(
                cur,
                """
                SELECT data_type
                FROM information_schema.columns
                WHERE table_name='users' AND column_name='accepted_policies'
                """,
            )
            row = _fetchone(cur)
            if row and row[0] != "boolean":
                db_execute(cur, "ALTER TABLE users ALTER COLUMN accepted_policies DROP DEFAULT")
                db_execute(
                    cur,
                    """
                    ALTER TABLE users
                    ALTER COLUMN accepted_policies TYPE boolean
                    USING (
                        CASE
                            WHEN accepted_policies IS NULL THEN FALSE
                            WHEN accepted_policies::text IN ('1','true','t','yes','y') THEN TRUE
                            ELSE FALSE
                        END
                    )
                    """,
                )
                db_execute(cur, "ALTER TABLE users ALTER COLUMN accepted_policies SET DEFAULT FALSE")
                conn.commit()
        except Exception:
            conn.rollback()

        # accepted_policies_at: if legacy text with empty strings, normalize
        try:
            db_execute(
                cur,
                """
                SELECT data_type
                FROM information_schema.columns
                WHERE table_name='users' AND column_name='accepted_policies_at'
                """,
            )
            row = _fetchone(cur)
            if row and row[0] != "timestamp with time zone":
                db_execute(cur, "UPDATE users SET accepted_policies_at = NULL WHERE accepted_policies_at::text = ''")
                db_execute(
                    cur,
                    """
                    ALTER TABLE users
                    ALTER COLUMN accepted_policies_at TYPE timestamptz
                    USING NULLIF(accepted_policies_at::text, '')::timestamptz
                    """,
                )
                conn.commit()
        except Exception:
            conn.rollback()

        conn.close()
        return

    # -------------------------
    # SQLite schema
    # -------------------------
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT,
            plan TEXT NOT NULL DEFAULT 'free',
            is_admin INTEGER NOT NULL DEFAULT 0,
            role TEXT NOT NULL DEFAULT 'user',
            is_banned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            upload_parses INTEGER NOT NULL DEFAULT 0,
            summary_uses INTEGER NOT NULL DEFAULT 0,
            cover_uses INTEGER NOT NULL DEFAULT 0,
            bullets_uses INTEGER NOT NULL DEFAULT 0,
            cv_generations INTEGER NOT NULL DEFAULT 0,
            reset_token TEXT,
            reset_token_created_at TEXT,
            referral_code TEXT UNIQUE,
            referred_by TEXT,
            referrals_count INTEGER NOT NULL DEFAULT 0,
            accepted_policies INTEGER NOT NULL DEFAULT 0,
            accepted_policies_at TEXT,
            job_summary_uses INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS credit_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL UNIQUE,
            cv_amount INTEGER NOT NULL DEFAULT 0,
            ai_amount INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS credit_spends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            cv_amount INTEGER NOT NULL DEFAULT 0,
            ai_amount INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )

    # SQLite migrations for users table
    cur.execute("PRAGMA table_info(users)")
    existing_cols = {row[1] for row in cur.fetchall()}

    migrations: List[str] = []
    if "plan" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
    if "is_admin" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    if "role" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
    if "is_banned" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0")
    if "created_at" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
    if "upload_parses" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN upload_parses INTEGER NOT NULL DEFAULT 0")
    if "summary_uses" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN summary_uses INTEGER NOT NULL DEFAULT 0")
    if "cover_uses" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN cover_uses INTEGER NOT NULL DEFAULT 0")
    if "bullets_uses" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN bullets_uses INTEGER NOT NULL DEFAULT 0")
    if "cv_generations" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN cv_generations INTEGER NOT NULL DEFAULT 0")
    if "reset_token" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN reset_token TEXT")
    if "reset_token_created_at" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN reset_token_created_at TEXT")
    if "referral_code" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN referral_code TEXT")
    if "referred_by" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN referred_by TEXT")
    if "referrals_count" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN referrals_count INTEGER NOT NULL DEFAULT 0")
    if "accepted_policies" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN accepted_policies INTEGER NOT NULL DEFAULT 0")
    if "accepted_policies_at" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN accepted_policies_at TEXT")
    if "job_summary_uses" not in existing_cols:
        migrations.append("ALTER TABLE users ADD COLUMN job_summary_uses INTEGER NOT NULL DEFAULT 0")

    for sql in migrations:
        cur.execute(sql)

    conn.commit()
    conn.close()


# -------------------------
# Row mapper
# -------------------------
def _row_to_user(row) -> Dict[str, Any]:
    if row is None:
        return {}

    cols = [
        "id",
        "email",
        "password_hash",
        "full_name",
        "plan",
        "is_admin",
        "role",
        "is_banned",
        "created_at",
        "upload_parses",
        "summary_uses",
        "cover_uses",
        "bullets_uses",
        "cv_generations",
        "reset_token",
        "reset_token_created_at",
        "referral_code",
        "referred_by",
        "referrals_count",
        "accepted_policies",
        "accepted_policies_at",
        "job_summary_uses",
    ]
    return dict(zip(cols, row))


# -------------------------
# Password + user ops
# -------------------------
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def create_user(
    email: str,
    password: str,
    full_name: Optional[str] = None,
    referred_by: Optional[str] = None,
) -> bool:
    """
    Create a new user. Returns False if email already exists.
    First user becomes owner/admin.
    """
    email = (email or "").strip().lower()
    if referred_by:
        referred_by = (referred_by or "").strip().upper()

    conn = get_conn()
    cur = conn.cursor()

    db_execute(cur, "SELECT id FROM users WHERE LOWER(email) = LOWER(?)", (email,))
    if _fetchone(cur):
        conn.close()
        return False

    db_execute(cur, "SELECT COUNT(*) FROM users")
    total = int(_fetchone(cur)[0] or 0)

    is_admin = 1 if total == 0 else 0
    role = "owner" if total == 0 else "user"

    now = datetime.now(timezone.utc).isoformat()
    pwd_hash = hash_password(password)

    # accepted_policies must match backend type (bool in PG, int in SQLite)
    accepted_policies_value = False if _is_postgres() else 0

    db_execute(
        cur,
        """
        INSERT INTO users
        (email, password_hash, full_name, plan, is_admin, role, created_at,
         referred_by, referrals_count, accepted_policies, accepted_policies_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            email,
            pwd_hash,
            full_name,
            "free",
            is_admin,
            role,
            now,
            referred_by,
            0,
            accepted_policies_value,
            None,
        ),
    )

    conn.commit()
    conn.close()
    return True


def _select_user_by_where(where_sql: str, params: tuple) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()

    db_execute(
        cur,
        f"""
        SELECT id, email, password_hash, full_name, plan, is_admin,
               role, is_banned,
               created_at, upload_parses, summary_uses, cover_uses,
               bullets_uses, cv_generations, reset_token,
               reset_token_created_at, referral_code, referred_by,
               referrals_count, accepted_policies, accepted_policies_at,
               job_summary_uses
        FROM users
        {where_sql}
        """,
        params,
    )

    row = _fetchone(cur)
    conn.close()
    if not row:
        return None

    user = _row_to_user(row)
    user.pop("password_hash", None)
    return user


def authenticate_user(email: str, password: str) -> Optional[Dict[str, Any]]:
    email = (email or "").strip().lower()
    pwd_hash = hash_password(password or "")

    user = _select_user_by_where(
        "WHERE LOWER(email) = LOWER(?) AND password_hash = ?",
        (email, pwd_hash),
    )

    if user and user.get("is_banned"):
        return None

    return user


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    email = (email or "").strip().lower()
    return _select_user_by_where("WHERE LOWER(email) = LOWER(?)", (email,))


def get_user_id_by_email(email: str) -> Optional[int]:
    email = (email or "").strip().lower()
    if not email:
        return None
    conn = get_conn()
    cur = conn.cursor()
    try:
        db_execute(cur, "SELECT id FROM users WHERE LOWER(email) = LOWER(?) LIMIT 1", (email,))
        row = _fetchone(cur)
        return int(row[0]) if row else None
    finally:
        conn.close()


# -------------------------
# Role & banning helpers (exports App.py expects)
# -------------------------
def set_role(email: str, role: str) -> None:
    email = (email or "").strip().lower()
    role = (role or "user").strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    try:
        db_execute(cur, "UPDATE users SET role = ? WHERE LOWER(email) = LOWER(?)", (role, email))
        conn.commit()
    finally:
        conn.close()


def set_banned(email: str, banned: bool) -> None:
    email = (email or "").strip().lower()
    val = 1 if banned else 0
    conn = get_conn()
    cur = conn.cursor()
    try:
        db_execute(cur, "UPDATE users SET is_banned = ? WHERE LOWER(email) = LOWER(?)", (val, email))
        conn.commit()
    finally:
        conn.close()


# -------------------------
# Usage & plan management
# -------------------------
USAGE_FIELDS = {
    "upload_parses",
    "summary_uses",
    "cover_uses",
    "bullets_uses",
    "cv_generations",
    "job_summary_uses",
}


def increment_usage(email: str, field: str, amount: int = 1) -> None:
    if field not in USAGE_FIELDS:
        return
    email = (email or "").strip().lower()

    conn = get_conn()
    cur = conn.cursor()
    try:
        db_execute(
            cur,
            f"UPDATE users SET {field} = COALESCE({field}, 0) + ? WHERE LOWER(email) = LOWER(?)",
            (int(amount), email),
        )
        conn.commit()
    finally:
        conn.close()


def set_plan(email: str, plan: str) -> None:
    email = (email or "").strip().lower()
    plan = (plan or "free").strip().lower()

    conn = get_conn()
    cur = conn.cursor()
    try:
        db_execute(cur, "UPDATE users SET plan = ? WHERE LOWER(email) = LOWER(?)", (plan, email))
        conn.commit()
    finally:
        conn.close()


def get_all_users() -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()

    db_execute(
        cur,
        """
        SELECT id, email, password_hash, full_name, plan, is_admin,
               role, is_banned,
               created_at, upload_parses, summary_uses, cover_uses,
               bullets_uses, cv_generations, reset_token,
               reset_token_created_at, referral_code, referred_by,
               referrals_count, accepted_policies, accepted_policies_at,
               job_summary_uses
        FROM users
        ORDER BY created_at DESC
        """,
    )

    rows = _fetchall(cur)
    conn.close()

    users: List[Dict[str, Any]] = []
    for row in rows:
        u = _row_to_user(row)
        u.pop("password_hash", None)
        users.append(u)
    return users


# -------------------------
# Password reset – token flow
# -------------------------
def create_password_reset_token(email: str) -> Optional[str]:
    email = (email or "").strip().lower()

    conn = get_conn()
    cur = conn.cursor()

    db_execute(cur, "SELECT id FROM users WHERE LOWER(email) = LOWER(?)", (email,))
    row = _fetchone(cur)
    if not row:
        conn.close()
        return None

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()

    db_execute(
        cur,
        "UPDATE users SET reset_token = ?, reset_token_created_at = ? WHERE LOWER(email) = LOWER(?)",
        (token, now, email),
    )
    conn.commit()
    conn.close()
    return token


def get_user_by_reset_token(token: str) -> Optional[Dict[str, Any]]:
    token = (token or "").strip()
    if not token:
        return None

    conn = get_conn()
    cur = conn.cursor()

    db_execute(
        cur,
        """
        SELECT id, email, password_hash, full_name, plan, is_admin,
               role, is_banned,
               created_at, upload_parses, summary_uses, cover_uses,
               bullets_uses, cv_generations, reset_token,
               reset_token_created_at, referral_code, referred_by,
               referrals_count, accepted_policies, accepted_policies_at,
               job_summary_uses
        FROM users
        WHERE reset_token = ?
        """,
        (token,),
    )

    row = _fetchone(cur)
    conn.close()
    if not row:
        return None

    user = _row_to_user(row)
    created_at_str = user.get("reset_token_created_at")
    if not created_at_str:
        return None

    try:
        created_at = datetime.fromisoformat(created_at_str)
    except Exception:
        return None

    if datetime.now(timezone.utc) - created_at.replace(tzinfo=timezone.utc) > timedelta(hours=RESET_TOKEN_EXPIRY_HOURS):
        return None

    user.pop("password_hash", None)
    return user


def clear_reset_token(email: str) -> None:
    email = (email or "").strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    try:
        db_execute(
            cur,
            "UPDATE users SET reset_token = NULL, reset_token_created_at = NULL WHERE LOWER(email) = LOWER(?)",
            (email,),
        )
        conn.commit()
    finally:
        conn.close()


def reset_password_with_token(token: str, new_password: str) -> bool:
    token = (token or "").strip()
    if not token:
        return False

    user = get_user_by_reset_token(token)
    if not user:
        return False

    email = user["email"]
    pwd_hash = hash_password(new_password or "")

    conn = get_conn()
    cur = conn.cursor()
    try:
        db_execute(
            cur,
            """
            UPDATE users
            SET password_hash = ?, reset_token = NULL, reset_token_created_at = NULL
            WHERE LOWER(email) = LOWER(?)
            """,
            (pwd_hash, email),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# -------------------------
# Referral helpers
# -------------------------
def _generate_referral_code() -> str:
    return secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10].upper()


def ensure_referral_code(email: str) -> str:
    email = (email or "").strip().lower()
    conn = get_conn()
    cur = conn.cursor()

    db_execute(cur, "SELECT referral_code FROM users WHERE LOWER(email) = LOWER(?)", (email,))
    row = _fetchone(cur)
    if not row:
        conn.close()
        raise ValueError("User not found")

    existing = row[0]
    if existing:
        conn.close()
        return str(existing).upper()

    while True:
        code = _generate_referral_code()
        db_execute(cur, "SELECT 1 FROM users WHERE UPPER(referral_code) = UPPER(?)", (code,))
        if not _fetchone(cur):
            break

    db_execute(cur, "UPDATE users SET referral_code = ? WHERE LOWER(email) = LOWER(?)", (code, email))
    conn.commit()
    conn.close()
    return code


def get_user_by_referral_code(code: str) -> Optional[Dict[str, Any]]:
    code = (code or "").strip().upper()
    if not code:
        return None

    conn = get_conn()
    cur = conn.cursor()

    db_execute(
        cur,
        """
        SELECT id, email, password_hash, full_name, plan, is_admin,
               role, is_banned,
               created_at, upload_parses, summary_uses, cover_uses,
               bullets_uses, cv_generations, reset_token,
               reset_token_created_at, referral_code, referred_by,
               referrals_count, accepted_policies, accepted_policies_at,
               job_summary_uses
        FROM users
        WHERE UPPER(referral_code) = UPPER(?)
        """,
        (code,),
    )

    row = _fetchone(cur)
    conn.close()
    if not row:
        return None

    user = _row_to_user(row)
    user.pop("password_hash", None)
    return user


def apply_referral_bonus(new_user_email: str, referral_code: str) -> bool:
    """
    Pays ONLY the referrer (+BONUS CV/AI) when a new user signs up with a referral code.
    Ledger-based: inserts into credit_grants using unique source 'referral_bonus:<referee_id>'.
    Idempotent + safe on retries.
    """
    email = (new_user_email or "").strip().lower()
    code = (referral_code or "").strip().upper()
    if not email or not code:
        return False

    cap = int(globals().get("REFERRAL_CAP", REFERRAL_CAP) or REFERRAL_CAP)
    bonus_cv = int(globals().get("BONUS_PER_REFERRAL_CV", BONUS_PER_REFERRAL_CV) or BONUS_PER_REFERRAL_CV)
    bonus_ai = int(globals().get("BONUS_PER_REFERRAL_AI", BONUS_PER_REFERRAL_AI) or BONUS_PER_REFERRAL_AI)

    conn = get_conn()
    cur = conn.cursor()
    try:
        # referee
        db_execute(cur, "SELECT id, referred_by FROM users WHERE LOWER(email)=LOWER(?) LIMIT 1", (email,))
        r = _fetchone(cur)
        if not r:
            return False
        referee_id, referred_by = int(r[0]), r[1]

        # referrer
        db_execute(cur, "SELECT id, COALESCE(referrals_count,0) FROM users WHERE UPPER(referral_code)=UPPER(?) LIMIT 1", (code,))
        rr = _fetchone(cur)
        if not rr:
            return False
        referrer_id, ref_cnt = int(rr[0]), int(rr[1] or 0)

        # always stamp referee.referred_by once
        if not referred_by:
            db_execute(cur, "UPDATE users SET referred_by=? WHERE id=?", (code, referee_id))

        if ref_cnt >= cap:
            conn.commit()
            return False

        source = f"referral_bonus:{referee_id}"

        inserted = False
        if _is_postgres():
            # Postgres: reliable idempotency check
            db_execute(
                cur,
                """
                INSERT INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at)
                VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT (source) DO NOTHING
                """,
                (referrer_id, source, bonus_cv, bonus_ai),
            )
            inserted = (cur.rowcount == 1)
        else:
            # SQLite: use INSERT OR IGNORE
            now_iso = datetime.now(timezone.utc).isoformat()
            db_execute(
                cur,
                """
                INSERT OR IGNORE INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at, created_at)
                VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (referrer_id, source, bonus_cv, bonus_ai, now_iso),
            )
            inserted = (cur.rowcount == 1)

        if inserted:
            db_execute(
                cur,
                "UPDATE users SET referrals_count = COALESCE(referrals_count,0) + 1 WHERE id=?",
                (referrer_id,),
            )

        conn.commit()
        return inserted

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print("apply_referral_bonus error:", repr(e))
        return False
    finally:
        conn.close()


# -------------------------
# Starter credits (new account bonus)
# -------------------------
def grant_starter_credits(user_id: int) -> bool:
    """
    Grants starter credits once per user via ledger (idempotent).
    source = 'starter_grant:<user_id>'
    """
    uid = int(user_id)
    source = f"starter_grant:{uid}"
    cv_amt = int(globals().get("STARTER_CV", STARTER_CV) or STARTER_CV)
    ai_amt = int(globals().get("STARTER_AI", STARTER_AI) or STARTER_AI)

    conn = get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres():
            db_execute(
                cur,
                """
                INSERT INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at)
                VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT (source) DO NOTHING
                """,
                (uid, source, cv_amt, ai_amt),
            )
            inserted = (cur.rowcount == 1)
        else:
            now_iso = datetime.now(timezone.utc).isoformat()
            db_execute(
                cur,
                """
                INSERT OR IGNORE INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at, created_at)
                VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (uid, source, cv_amt, ai_amt, now_iso),
            )
            inserted = (cur.rowcount == 1)

        conn.commit()
        return inserted
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print("grant_starter_credits error:", repr(e))
        return False
    finally:
        conn.close()


# -------------------------
# Policy acceptance helpers
# -------------------------

from db import fetchone, execute


def has_accepted_policies(email: str) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False

    row = fetchone(
        """
        SELECT accepted_policies, accepted_policies_at
        FROM users
        WHERE LOWER(email) = LOWER(%s)
        LIMIT 1
        """,
        (email,),
    )
    if not row:
        return False

    return bool(row.get("accepted_policies") or row.get("accepted_policies_at"))


def mark_policies_accepted(email: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return

    execute(
        """
        UPDATE users
        SET accepted_policies = TRUE,
            accepted_policies_at = COALESCE(accepted_policies_at, NOW())
        WHERE LOWER(email) = LOWER(%s)
        """,
        (email,),
    )



# -------------------------
# Delete helpers
# -------------------------
def delete_user(email: str) -> None:
    email = (email or "").strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    try:
        db_execute(cur, "DELETE FROM users WHERE LOWER(email) = LOWER(?)", (email,))
        conn.commit()
    finally:
        conn.close()
