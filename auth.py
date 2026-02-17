import os
import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

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


def apply_referral_bonus(new_user_email: str, referral_code: str) -> bool:
    """
    Pays ONLY the referrer (+5 CV, +5 AI) when a new user signs up with a referral code.
    Ledger-based: inserts into credit_grants using unique source 'referral_bonus:<new_user_id>'.
    """
    email = (new_user_email or "").strip().lower()
    code = (referral_code or "").strip().upper()
    if not email or not code:
        return False

    conn = get_conn()
    cur = conn.cursor()
    try:
        # referee
        db_execute(cur, "SELECT id, referred_by FROM users WHERE lower(email)=? LIMIT 1", (email,))
        r = _fetchone(cur)
        if not r:
            return False
        referee_id, referred_by = r[0], r[1]

        # referrer
        db_execute(cur, "SELECT id, referrals_count FROM users WHERE referral_code=? LIMIT 1", (code,))
        rr = _fetchone(cur)
        if not rr:
            return False
        referrer_id, ref_cnt = int(rr[0]), int(rr[1] or 0)

        # constants
        cap = globals().get("REFERRAL_CAP", 10)
        bonus_cv = globals().get("BONUS_PER_REFERRAL_CV", 5)
        bonus_ai = globals().get("BONUS_PER_REFERRAL_AI", 5)

        # always set referee.referred_by once
        if not referred_by:
            db_execute(cur, "UPDATE users SET referred_by=? WHERE id=?", (code, referee_id))

        if ref_cnt >= cap:
            conn.commit()
            return False

        # ledger grant (idempotent)
        db_execute(
            cur,
            """
            INSERT INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at)
            VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT (source) DO NOTHING
            """,
            (referrer_id, f"referral_bonus:{referee_id}", bonus_cv, bonus_ai),
        )

        # if grant exists, bump count
        db_execute(cur, "SELECT 1 FROM credit_grants WHERE source=? LIMIT 1", (f"referral_bonus:{referee_id}",))
        if _fetchone(cur):
            db_execute(
                cur,
                "UPDATE users SET referrals_count = COALESCE(referrals_count,0) + 1 WHERE id=?",
                (referrer_id,),
            )

        conn.commit()
        return True

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
# Schema init
# -------------------------
def init_db():
    """
    Initialise DB schema.
    Safe to call on every run.
    SQLite: also performs migrations.
    Postgres: creates table if missing + performs lightweight type migrations.
    """
    conn = get_conn()
    cur = conn.cursor()

    # --- Postgres ---
    if _is_postgres():
        # Create with the CORRECT types for prod
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
        conn.commit()

        # ---- Lightweight migrations (don’t crash if already correct) ----
        try:
            # Ensure accepted_policies is boolean
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
                # Drop default first, then convert int->bool
                db_execute(cur, "ALTER TABLE users ALTER COLUMN accepted_policies DROP DEFAULT")
                db_execute(
                    cur,
                    """
                    ALTER TABLE users
                    ALTER COLUMN accepted_policies TYPE boolean
                    USING (accepted_policies::int = 1)
                    """,
                )
                db_execute(cur, "ALTER TABLE users ALTER COLUMN accepted_policies SET DEFAULT FALSE")
                conn.commit()
        except Exception:
            conn.rollback()

        try:
            # Ensure accepted_policies_at is timestamptz (and clear empty strings)
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
                # if text exists, empty string needs to become NULL first
                db_execute(cur, "UPDATE users SET accepted_policies_at = NULL WHERE accepted_policies_at = ''")
                db_execute(
                    cur,
                    """
                    ALTER TABLE users
                    ALTER COLUMN accepted_policies_at TYPE timestamptz
                    USING NULLIF(accepted_policies_at, '')::timestamptz
                    """,
                )
                conn.commit()
        except Exception:
            conn.rollback()

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
    """
    email = email.strip().lower()
    if referred_by:
        referred_by = referred_by.strip().lower()

    conn = get_conn()
    cur = conn.cursor()

    db_execute(cur, "SELECT id FROM users WHERE email = ?", (email,))
    if _fetchone(cur):
        conn.close()
        return False

    db_execute(cur, "SELECT COUNT(*) FROM users")
    total = _fetchone(cur)[0]
    is_admin = 1 if total == 0 else 0
    role = "owner" if total == 0 else "user"

    now = datetime.utcnow().isoformat()
    pwd_hash = hash_password(password)

    # IMPORTANT: accepted_policies must match backend type
    accepted_policies_value = False if _is_postgres() else 0
    accepted_policies_at_value = None

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
            accepted_policies_at_value,
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
    email = email.strip().lower()
    pwd_hash = hash_password(password)

    user = _select_user_by_where(
        "WHERE email = ? AND password_hash = ?",
        (email, pwd_hash),
    )

    if user and user.get("is_banned"):
        return None

    return user


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    email = email.strip().lower()
    return _select_user_by_where("WHERE email = ?", (email,))


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

    if datetime.utcnow() - created_at > timedelta(hours=RESET_TOKEN_EXPIRY_HOURS):
        return None

    user.pop("password_hash", None)
    return user


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


def get_user_by_referral_code(code: str) -> Optional[Dict[str, Any]]:
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


# -------------------------
# Policy acceptance helpers
# -------------------------
def has_accepted_policies(email: str) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False

    conn = get_conn()
    cur = conn.cursor()
    try:
        db_execute(
            cur,
            """
            SELECT accepted_policies, accepted_policies_at
            FROM users
            WHERE email = ?
            LIMIT 1
            """,
            (email,),
        )
        row = _fetchone(cur)
        if not row:
            return False

        accepted_policies, accepted_at = row

        # Normalize SQLite (0/1) + Postgres (True/False)
        if isinstance(accepted_policies, bool):
            accepted_flag = accepted_policies
        elif accepted_policies is None:
            accepted_flag = False
        else:
            accepted_flag = str(accepted_policies).lower() in {"1", "true", "t", "yes", "y"}

        # accepted_at may be text (sqlite) or datetime (pg)
        accepted_at_flag = accepted_at is not None and str(accepted_at).strip() != ""

        return bool(accepted_flag or accepted_at_flag)
    finally:
        conn.close()


def mark_policies_accepted(email: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return

    conn = get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres():
            # Postgres: boolean + timestamptz
            db_execute(
                cur,
                """
                UPDATE users
                SET
                    accepted_policies = TRUE,
                    accepted_policies_at = COALESCE(accepted_policies_at, NOW())
                WHERE email = ?
                """,
                (email,),
            )
        else:
            # SQLite: int + text timestamp
            db_execute(
                cur,
                """
                UPDATE users
                SET
                    accepted_policies = 1,
                    accepted_policies_at = COALESCE(accepted_policies_at, ?)
                WHERE email = ?
                """,
                (datetime.utcnow().isoformat(), email),
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
