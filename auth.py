# auth.py
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db import execute, fetchone, fetchall, is_postgres

DB_PATH = "users.db"
RESET_TOKEN_EXPIRY_HOURS = 2

# Referral defaults
REFERRAL_CAP = 10
BONUS_PER_REFERRAL_CV = 5
BONUS_PER_REFERRAL_AI = 5

STARTER_CV = 5
STARTER_AI = 5


# -------------------------
# Schema init
# -------------------------
def init_db() -> None:
    """
    Create tables if missing.
    Uses dialect-specific DDL (sqlite vs postgres).
    """
    if is_postgres():
        execute(
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
            """
        )

        execute(
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
            """
        )

        execute(
            """
            CREATE TABLE IF NOT EXISTS credit_spends (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                cv_amount INTEGER NOT NULL DEFAULT 0,
                ai_amount INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # idempotency constraint
        try:
            execute(
                "ALTER TABLE credit_grants ADD CONSTRAINT credit_grants_source_key UNIQUE (source);"
            )
        except Exception:
            # already exists
            pass

        return

    # SQLite DDL
    execute(
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
        );
        """
    )

    execute(
        """
        CREATE TABLE IF NOT EXISTS credit_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL UNIQUE,
            cv_amount INTEGER NOT NULL DEFAULT 0,
            ai_amount INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            created_at TEXT NOT NULL
        );
        """
    )

    execute(
        """
        CREATE TABLE IF NOT EXISTS credit_spends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            cv_amount INTEGER NOT NULL DEFAULT 0,
            ai_amount INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )


# -------------------------
# Password + user ops
# -------------------------
def hash_password(password: str) -> str:
    return hashlib.sha256((password or "").encode("utf-8")).hexdigest()


def create_user(
    email: str,
    password: str,
    full_name: Optional[str] = None,
    referred_by: Optional[str] = None,
) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False

    referred_by_code = (referred_by or "").strip().upper() or None

    exists = fetchone("SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
    if exists:
        return False

    row = fetchone("SELECT COUNT(*) AS c FROM users", ())
    total = int((row or {}).get("c", 0) or 0)

    is_admin = 1 if total == 0 else 0
    role = "owner" if total == 0 else "user"
    now = datetime.now(timezone.utc).isoformat()
    pwd_hash = hash_password(password)

    if is_postgres():
        execute(
            """
            INSERT INTO users
                (email, password_hash, full_name, plan, is_admin, role, created_at,
                 referred_by, referrals_count, accepted_policies, accepted_policies_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s)
            """,
            (
                email,
                pwd_hash,
                full_name,
                "free",
                is_admin,
                role,
                now,
                referred_by_code,
                0,
                False,
                None,
            ),
        )
    else:
        execute(
            """
            INSERT INTO users
                (email, password_hash, full_name, plan, is_admin, role, created_at,
                 referred_by, referrals_count, accepted_policies, accepted_policies_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s)
            """,
            (
                email,
                pwd_hash,
                full_name,
                "free",
                is_admin,
                role,
                now,
                referred_by_code,
                0,
                0,
                None,
            ),
        )

    return True


def authenticate_user(email: str, password: str) -> Optional[Dict[str, Any]]:
    email = (email or "").strip().lower()
    pwd_hash = hash_password(password)

    user = fetchone(
        """
        SELECT *
        FROM users
        WHERE LOWER(email)=LOWER(%s) AND password_hash=%s
        LIMIT 1
        """,
        (email, pwd_hash),
    )
    if not user:
        return None

    if bool(user.get("is_banned")):
        return None

    user.pop("password_hash", None)
    return dict(user)


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    email = (email or "").strip().lower()
    if not email:
        return None
    user = fetchone("SELECT * FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
    if not user:
        return None
    u = dict(user)
    u.pop("password_hash", None)
    return u


def get_all_users() -> List[Dict[str, Any]]:
    rows = fetchall("SELECT * FROM users ORDER BY created_at DESC", ())
    out: List[Dict[str, Any]] = []
    for r in rows:
        u = dict(r)
        u.pop("password_hash", None)
        out.append(u)
    return out


def get_user_id_by_email(email: str) -> Optional[int]:
    row = fetchone("SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", ((email or "").strip().lower(),))
    return int(row["id"]) if row and row.get("id") is not None else None


# -------------------------
# Role & banning helpers (App.py imports these)
# -------------------------
def set_role(email: str, role: str) -> None:
    email = (email or "").strip().lower()
    role = (role or "user").strip().lower()
    execute("UPDATE users SET role=%s WHERE LOWER(email)=LOWER(%s)", (role, email))


def set_banned(email: str, banned: bool) -> None:
    email = (email or "").strip().lower()
    execute("UPDATE users SET is_banned=%s WHERE LOWER(email)=LOWER(%s)", (1 if banned else 0, email))


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
    execute(
        f"UPDATE users SET {field} = COALESCE({field},0) + %s WHERE LOWER(email)=LOWER(%s)",
        (int(amount), email),
    )


def set_plan(email: str, plan: str) -> None:
    email = (email or "").strip().lower()
    plan = (plan or "free").strip().lower()
    execute("UPDATE users SET plan=%s WHERE LOWER(email)=LOWER(%s)", (plan, email))


# -------------------------
# Password reset token flow
# -------------------------
def create_password_reset_token(email: str) -> Optional[str]:
    email = (email or "").strip().lower()
    row = fetchone("SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
    if not row:
        return None

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()

    execute(
        "UPDATE users SET reset_token=%s, reset_token_created_at=%s WHERE LOWER(email)=LOWER(%s)",
        (token, now, email),
    )
    return token


def _get_user_by_reset_token(token: str) -> Optional[Dict[str, Any]]:
    token = (token or "").strip()
    if not token:
        return None

    user = fetchone("SELECT * FROM users WHERE reset_token=%s LIMIT 1", (token,))
    if not user:
        return None

    created_at_str = user.get("reset_token_created_at")
    if not created_at_str:
        return None

    try:
        created_at = datetime.fromisoformat(created_at_str)
    except Exception:
        return None

    if datetime.now(timezone.utc) - created_at.replace(tzinfo=timezone.utc) > timedelta(hours=RESET_TOKEN_EXPIRY_HOURS):
        return None

    return dict(user)


def reset_password_with_token(token: str, new_password: str) -> bool:
    user = _get_user_by_reset_token(token)
    if not user:
        return False

    email = user["email"]
    pwd_hash = hash_password(new_password)

    execute(
        """
        UPDATE users
        SET password_hash=%s, reset_token=NULL, reset_token_created_at=NULL
        WHERE LOWER(email)=LOWER(%s)
        """,
        (pwd_hash, email),
    )
    return True


# -------------------------
# Referral helpers
# -------------------------
def _generate_referral_code() -> str:
    return secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10].upper()


def ensure_referral_code(email: str) -> str:
    email = (email or "").strip().lower()
    row = fetchone("SELECT referral_code FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
    if not row:
        raise ValueError("User not found")

    existing = (row.get("referral_code") or "").strip()
    if existing:
        return existing.upper()

    while True:
        code = _generate_referral_code()
        taken = fetchone("SELECT 1 AS x FROM users WHERE UPPER(referral_code)=UPPER(%s) LIMIT 1", (code,))
        if not taken:
            break

    execute("UPDATE users SET referral_code=%s WHERE LOWER(email)=LOWER(%s)", (code, email))
    return code


def get_user_by_referral_code(code: str) -> Optional[Dict[str, Any]]:
    code = (code or "").strip().upper()
    if not code:
        return None
    user = fetchone("SELECT * FROM users WHERE UPPER(referral_code)=UPPER(%s) LIMIT 1", (code,))
    if not user:
        return None
    u = dict(user)
    u.pop("password_hash", None)
    return u


def apply_referral_bonus(new_user_email: str, referral_code: str) -> bool:
    """
    Pays ONLY the referrer when a new user signs up with a referral code.
    Ledger-based: credit_grants unique source 'referral_bonus:<referee_id>'
    """
    email = (new_user_email or "").strip().lower()
    code = (referral_code or "").strip().upper()
    if not email or not code:
        return False

    cap = int(globals().get("REFERRAL_CAP", REFERRAL_CAP))
    bonus_cv = int(globals().get("BONUS_PER_REFERRAL_CV", BONUS_PER_REFERRAL_CV))
    bonus_ai = int(globals().get("BONUS_PER_REFERRAL_AI", BONUS_PER_REFERRAL_AI))

    referee = fetchone("SELECT id, referred_by FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
    if not referee:
        return False
    referee_id = int(referee["id"])
    referred_by = referee.get("referred_by")

    referrer = fetchone(
        "SELECT id, COALESCE(referrals_count,0) AS cnt FROM users WHERE UPPER(referral_code)=UPPER(%s) LIMIT 1",
        (code,),
    )
    if not referrer:
        return False

    referrer_id = int(referrer["id"])
    ref_cnt = int(referrer.get("cnt") or 0)

    # stamp referee.referred_by once
    if not referred_by:
        execute("UPDATE users SET referred_by=%s WHERE id=%s", (code, referee_id))

    if ref_cnt >= cap:
        return False

    source = f"referral_bonus:{referee_id}"

    if is_postgres():
        execute(
            """
            INSERT INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at)
            VALUES (%s, %s, %s, %s, NULL)
            ON CONFLICT (source) DO NOTHING
            """,
            (referrer_id, source, bonus_cv, bonus_ai),
        )
    else:
        now_iso = datetime.now(timezone.utc).isoformat()
        execute(
            """
            INSERT OR IGNORE INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at, created_at)
            VALUES (%s, %s, %s, %s, NULL, %s)
            """,
            (referrer_id, source, bonus_cv, bonus_ai, now_iso),
        )

    # only increment referrals_count if it actually exists now
    exists = fetchone("SELECT 1 AS x FROM credit_grants WHERE source=%s LIMIT 1", (source,))
    if exists:
        execute("UPDATE users SET referrals_count = COALESCE(referrals_count,0) + 1 WHERE id=%s", (referrer_id,))
        return True

    return False


# -------------------------
# Starter credits
# -------------------------
def grant_starter_credits(user_id: int) -> bool:
    uid = int(user_id)
    source = f"starter_grant:{uid}"
    cv_amt = int(globals().get("STARTER_CV", STARTER_CV))
    ai_amt = int(globals().get("STARTER_AI", STARTER_AI))

    if is_postgres():
        execute(
            """
            INSERT INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at)
            VALUES (%s, %s, %s, %s, NULL)
            ON CONFLICT (source) DO NOTHING
            """,
            (uid, source, cv_amt, ai_amt),
        )
    else:
        now_iso = datetime.now(timezone.utc).isoformat()
        execute(
            """
            INSERT OR IGNORE INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at, created_at)
            VALUES (%s, %s, %s, %s, NULL, %s)
            """,
            (uid, source, cv_amt, ai_amt, now_iso),
        )

    return bool(fetchone("SELECT 1 AS x FROM credit_grants WHERE source=%s LIMIT 1", (source,)))


# -------------------------
# Policies
# -------------------------
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

    if is_postgres():
        execute(
            """
            UPDATE users
            SET accepted_policies = TRUE,
                accepted_policies_at = COALESCE(accepted_policies_at, NOW())
            WHERE LOWER(email) = LOWER(%s)
            """,
            (email,),
        )
    else:
        now_iso = datetime.now(timezone.utc).isoformat()
        execute(
            """
            UPDATE users
            SET accepted_policies = 1,
                accepted_policies_at = COALESCE(accepted_policies_at, %s)
            WHERE LOWER(email) = LOWER(%s)
            """,
            (now_iso, email),
        )


# -------------------------
# Delete
# -------------------------
def delete_user(email: str) -> None:
    email = (email or "").strip().lower()
    execute("DELETE FROM users WHERE LOWER(email)=LOWER(%s)", (email,))
