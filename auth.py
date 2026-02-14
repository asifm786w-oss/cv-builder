import os
import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, List

DB_PATH = "users.db"

# How long a reset token is valid for (hours)
RESET_TOKEN_EXPIRY_HOURS = 2


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
        return sqlite3.connect(DB_PATH)

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


# -------------------------
# Row mapper
# -------------------------
def _row_to_user(row) -> Dict:
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
# Schema init
# -------------------------
def init_db():
    """
    Initialise DB schema.
    Safe to call on every run.
    SQLite: also performs migrations.
    Postgres: creates table if missing.
    """
    conn = get_conn()
    cur = conn.cursor()

    # --- Postgres ---
    if _is_postgres():
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
                accepted_policies INTEGER NOT NULL DEFAULT 0,
                accepted_policies_at TEXT,
                job_summary_uses INTEGER NOT NULL DEFAULT 0
            );
            """,
        )
        conn.commit()
        conn.close()
        return

    # --- SQLite ---
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

    # SQLite migrations
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
    First user in the DB is automatically made admin/owner.
    Optionally store who referred them (by email).
    """
    email = email.strip().lower()
    if referred_by:
        referred_by = referred_by.strip().lower()

    conn = get_conn()
    cur = conn.cursor()

    # Is this email already registered?
    db_execute(cur, "SELECT id FROM users WHERE email = ?", (email,))
    if _fetchone(cur):
        conn.close()
        return False

    # Count how many users exist to decide admin/owner flags
    db_execute(cur, "SELECT COUNT(*) FROM users")
    total = _fetchone(cur)[0]
    is_admin = 1 if total == 0 else 0
    role = "owner" if total == 0 else "user"

    now = datetime.utcnow().isoformat()
    pwd_hash = hash_password(password)

    db_execute(
        cur,
        """
        INSERT INTO users
        (email, password_hash, full_name, plan, is_admin, role, created_at,
         referred_by, referrals_count, accepted_policies, accepted_policies_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (email, pwd_hash, full_name, "free", is_admin, role, now, referred_by, 0, 0, None),
    )

    conn.commit()
    conn.close()
    return True


def _select_user_by_where(where_sql: str, params: tuple) -> Optional[Dict]:
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


def authenticate_user(email: str, password: str) -> Optional[Dict]:
    email = email.strip().lower()
    pwd_hash = hash_password(password)

    user = _select_user_by_where(
        "WHERE email = ? AND password_hash = ?",
        (email, pwd_hash),
    )

    if user and user.get("is_banned"):
        return None

    return user


def get_user_by_email(email: str) -> Optional[Dict]:
    email = email.strip().lower()
    return _select_user_by_where("WHERE email = ?", (email,))


# -------------------------
# Role & banning helpers
# -------------------------
def set_role(email: str, role: str) -> None:
    email = email.strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    db_execute(cur, "UPDATE users SET role = ? WHERE email = ?", (role, email))
    conn.commit()
    conn.close()


def set_banned(email: str, banned: bool) -> None:
    email = email.strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    db_execute(cur, "UPDATE users SET is_banned = ? WHERE email = ?", (1 if banned else 0, email))
    conn.commit()
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

    email = email.strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    db_execute(
        cur,
        f"""
        UPDATE users
        SET {field} = {field} + ?
        WHERE email = ?
        """,
        (amount, email),
    )
    conn.commit()
    conn.close()


def set_plan(email: str, plan: str) -> None:
    email = email.strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    db_execute(cur, "UPDATE users SET plan = ? WHERE email = ?", (plan, email))
    conn.commit()
    conn.close()


def get_all_users() -> List[Dict]:
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

    users = []
    for row in rows:
        u = _row_to_user(row)
        u.pop("password_hash", None)
        users.append(u)
    return users


# -------------------------
# Password reset – token flow
# -------------------------
def create_password_reset_token(email: str) -> Optional[str]:
    email = email.strip().lower()

    conn = get_conn()
    cur = conn.cursor()

    db_execute(cur, "SELECT id FROM users WHERE email = ?", (email,))
    row = _fetchone(cur)
    if not row:
        conn.close()
        return None

    token = secrets.token_urlsafe(32)
    now = datetime.utcnow().isoformat()

    db_execute(
        cur,
        """
        UPDATE users
        SET reset_token = ?, reset_token_created_at = ?
        WHERE email = ?
        """,
        (token, now, email),
    )
    conn.commit()
    conn.close()
    return token


def get_user_by_reset_token(token: str) -> Optional[Dict]:
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

    if datetime.utcnow() - created_at > timedelta(hours=RESET_TOKEN_EXPIRY_HOURS):
        return None

    user.pop("password_hash", None)
    return user


def clear_reset_token(email: str) -> None:
    email = email.strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    db_execute(
        cur,
        """
        UPDATE users
        SET reset_token = NULL,
            reset_token_created_at = NULL
        WHERE email = ?
        """,
        (email,),
    )
    conn.commit()
    conn.close()


def reset_password_with_token(token: str, new_password: str) -> bool:
    token = (token or "").strip()
    if not token:
        return False

    user = get_user_by_reset_token(token)
    if not user:
        return False

    email = user["email"]
    pwd_hash = hash_password(new_password)

    conn = get_conn()
    cur = conn.cursor()
    db_execute(
        cur,
        """
        UPDATE users
        SET password_hash = ?, reset_token = NULL, reset_token_created_at = NULL
        WHERE email = ?
        """,
        (pwd_hash, email),
    )
    conn.commit()
    updated = cur.rowcount
    conn.close()
    return updated > 0


# -------------------------
# Referral helpers
# -------------------------
def _generate_referral_code() -> str:
    return secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10].upper()


def ensure_referral_code(email: str) -> str:
    email = email.strip().lower()
    conn = get_conn()
    cur = conn.cursor()

    db_execute(cur, "SELECT referral_code FROM users WHERE email = ?", (email,))
    row = _fetchone(cur)
    if not row:
        conn.close()
        raise ValueError("User not found")

    existing = row[0]
    if existing:
        conn.close()
        return existing

    while True:
        code = _generate_referral_code()
        db_execute(cur, "SELECT 1 FROM users WHERE referral_code = ?", (code,))
        if not _fetchone(cur):
            break

    db_execute(cur, "UPDATE users SET referral_code = ? WHERE email = ?", (code, email))
    conn.commit()
    conn.close()
    return code


def get_user_by_referral_code(code: str) -> Optional[Dict]:
    code = (code or "").strip()
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
        WHERE referral_code = ?
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


def apply_referral_bonus(referrer_email: str, max_referrals: int = 10) -> None:
    email = referrer_email.strip().lower()
    conn = get_conn()
    cur = conn.cursor()

    db_execute(cur, "SELECT referrals_count FROM users WHERE email = ?", (email,))
    row = _fetchone(cur)
    if not row:
        conn.close()
        return

    current = row[0] or 0
    if current >= max_referrals:
        conn.close()
        return

    new_count = current + 1
    db_execute(cur, "UPDATE users SET referrals_count = ? WHERE email = ?", (new_count, email))
    conn.commit()
    conn.close()


from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone

# -------------------------
# Policies consent helpers (Postgres)
# -------------------------
def has_accepted_policies(email: str) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(accepted_policies, FALSE) AS accepted_policies,
                    accepted_policies_at
                FROM users
                WHERE email = %s
                """,
                (email,),
            )
            row = cur.fetchone()
            if not row:
                return False

            if bool(row.get("accepted_policies")):
                return True

            return row.get("accepted_policies_at") is not None
    finally:
        conn.close()


def mark_policies_accepted(email: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET
                    accepted_policies = TRUE,
                    accepted_policies_at = COALESCE(accepted_policies_at, NOW())
                WHERE email = %s
                """,
                (email,),
            )
        conn.commit()
    finally:
        conn.close()



# -------------------------
# Delete helpers
# -------------------------
def delete_user(email: str) -> None:
    email = email.strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    try:
        db_execute(cur, "DELETE FROM users WHERE email = ?", (email,))
        conn.commit()
    finally:
        conn.close()
