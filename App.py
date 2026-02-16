import streamlit as st
import io
import csv
import os
import hashlib
import time
import traceback
import requests
import psycopg2
import stripe
import psycopg2.extras
import datetime


from datetime import datetime, timezone
from psycopg2.extras import RealDictCursor
from openai import OpenAI
from adzuna_client import search_jobs
from datetime import datetime, timezone


from utils import verify_postgres_connection
from models import CV, Experience, Education
from utils import (
    render_cv_pdf_bytes,
    render_cover_letter_pdf_bytes,
    render_cv_docx_bytes,
    render_cover_letter_docx_bytes,
)
from ai_v2 import (
    generate_tailored_summary,
    generate_cover_letter_ai,
    improve_bullets,
    extract_cv_data,
    generate_job_summary,
)
from auth import (
    init_db,
    create_user,
    authenticate_user,
    increment_usage,
    get_all_users,
    set_plan,
    get_user_by_email,
    create_password_reset_token,
    reset_password_with_token,
    ensure_referral_code,
    get_user_by_referral_code,
    apply_referral_bonus,
    has_accepted_policies,
    mark_policies_accepted,
    set_role,
    set_banned,
    delete_user,
)

from email_utils import send_password_reset_email


# -------------------------
# PAGE CONFIG (MUST BE FIRST st.* CALL)
# -------------------------
st.set_page_config(
    page_title="Mulyba",
    page_icon="📄",
    layout="centered",
    initial_sidebar_state="expanded",
)


st.set_page_config(layout="centered")

st.markdown(
    """
    <style>
    /* ===== Mobile layout fixes ===== */
    @media (max-width: 768px) {

        /* Hide the marketing rail on phones */
        #mulyba-rail {
            display: none !important;
        }

        /* Reduce Streamlit padding on mobile */
        section[data-testid="stMain"] > div {
            padding-left: 0.75rem !important;
            padding-right: 0.75rem !important;
            padding-top: 0.75rem !important;
        }

        .block-container {
            max-width: 100% !important;
            padding-left: 0.75rem !important;
            padding-right: 0.75rem !important;
        }

        /* Full-width inputs & buttons on mobile */
        input, textarea, button {
            width: 100% !important;
        }

        h1 { font-size: 1.8rem !important; }
        h2 { font-size: 1.4rem !important; }
        h3 { font-size: 1.15rem !important; }
    }
    </style>
    """,
    unsafe_allow_html=True
)







# -------------------------
# GLOBAL PLAN + REFERRAL CONFIG
# -------------------------
REFERRAL_CAP = 10
BONUS_PER_REFERRAL_CV = 5
BONUS_PER_REFERRAL_AI = 5

PLAN_LIMITS = {
    "free": {"cv": 5, "ai": 5},
    "monthly": {"cv": 20, "ai": 30},
    "pro": {"cv": 50, "ai": 90},
    "one_time": {"cv": 40, "ai": 60},
    "yearly": {"cv": 300, "ai": 600},
    "premium": {"cv": 5000, "ai": 10000},
    "enterprise": {"cv": 5000, "ai": 10000},
}

USAGE_KEYS_DEFAULTS = {
    "upload_parses": 0,
    "summary_uses": 0,
    "cover_uses": 0,
    "bullets_uses": 0,
    "cv_generations": 0,
    "job_summary_uses": 0,
}

AI_USAGE_KEYS = {"summary_uses", "cover_uses", "bullets_uses", "job_summary_uses"}
CV_USAGE_KEYS = {"cv_generations"}

COOLDOWN_SECONDS = 5



def get_user_id(email: str) -> int | None:
    u = get_user_by_email(email)
    return int(u["id"]) if u and u.get("id") is not None else None

def init_db():
    conn = get_db_connection()
    try:
        ensure_credit_tables(conn)
    finally:
        conn.close()

def get_personal_value(primary_key: str, fallback_key: str) -> str:
    return (st.session_state.get(primary_key) or st.session_state.get(fallback_key) or "").strip()


def get_user_row_by_id(user_id: int) -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s LIMIT 1", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None

def get_db_connection():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")

    return psycopg2.connect(
        dsn,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )

def get_user_by_email(email: str):
    email = (email or "").strip().lower()
    if not email:
        return None
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
        return cur.fetchone()

def get_conn():
    return get_db_connection()

def refresh_session_user_from_db() -> None:
    """Refresh st.session_state['user'] from DB using the user id."""
    u = st.session_state.get("user") or {}
    uid = u.get("id")
    if not uid:
        return
    db_u = get_user_row_by_id(int(uid))
    if db_u:
        # keep anything you store only in session (optional)
        for k in ("role",):
            if k in u and k not in db_u:
                db_u[k] = u[k]
        st.session_state["user"] = db_u



def get_credits_by_user_id(user_id: int) -> dict:
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
              GREATEST(
                COALESCE((SELECT SUM(cv_amount) FROM credit_grants
                          WHERE user_id=%s AND (expires_at IS NULL OR expires_at > NOW())), 0)
                -
                COALESCE((SELECT SUM(cv_amount) FROM credit_spends WHERE user_id=%s), 0),
              0) AS cv,
              GREATEST(
                COALESCE((SELECT SUM(ai_amount) FROM credit_grants
                          WHERE user_id=%s AND (expires_at IS NULL OR expires_at > NOW())), 0)
                -
                COALESCE((SELECT SUM(ai_amount) FROM credit_spends WHERE user_id=%s), 0),
              0) AS ai
            """,
            (user_id, user_id, user_id, user_id),
        )
        row = cur.fetchone() or {}
        return {"cv": int(row["cv"]), "ai": int(row["ai"])}

def improve_skills(skills_text: str) -> str:
    """
    Skills-only AI improvement.
    Uses the same AI pipeline as improve_bullets(), then your normalizer
    will convert output into clean 1–3 word skill bullets.
    """
    return improve_bullets(skills_text)

def clear_ai_upload_state_only():
    """
    Remove only AI/upload/parse leftovers so they don't leak into CV output.
    DO NOT touch cv_* keys (your form fields).
    """
    for k in list(st.session_state.keys()):
        if k.startswith(("ai_", "upload_", "parsed_", "adzuna_", "job_")):
            st.session_state.pop(k, None)



def get_cv_field(key: str, fallback=None):
    """Read CV-only session state first, else fall back to existing variable/value."""
    v = st.session_state.get(key, None)
    return fallback if (v is None or v == "") else v


def set_cv_defaults_from_existing(full_name=None, title=None, email=None, phone=None, location=None, summary=None):
    """
    One-time migration: if you already had values in old variables, copy them into cv_* keys.
    Won't overwrite if cv_* already set.
    """
    defaults = {
        "cv_full_name": full_name,
        "cv_title": title,
        "cv_email": email,
        "cv_phone": phone,
        "cv_location": location,
        "cv_summary": summary,
    }
    for k, v in defaults.items():
        if (st.session_state.get(k) is None or st.session_state.get(k) == "") and v:
            st.session_state[k] = v


def try_spend(user_id: int, source: str, cv: int = 0, ai: int = 0) -> bool:
    cv = int(cv or 0)
    ai = int(ai or 0)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user_id,))
            if not cur.fetchone():
                return False

            bal = get_credits_by_user_id(user_id)

            if cv > 0 and bal["cv"] < cv:
                return False
            if ai > 0 and bal["ai"] < ai:
                return False

            cur.execute(
                """
                INSERT INTO credit_spends (user_id, source, cv_amount, ai_amount)
                VALUES (%s, %s, %s, %s)
                """,
                (user_id, source, cv, ai),
            )
        conn.commit()
        return True



def apply_referral_bonus(new_user_email: str, referral_code: str) -> bool:
    """
    Pays ONLY the referrer (+5 CV, +5 AI) when a new user signs up with a referral code.
    Marks the new user referral_bonus_applied=TRUE to prevent double pay.
    Uses your actual DB columns: cv_credits / ai_credits / referrals_count.
    psycopg2 version.
    """
    email = (new_user_email or "").strip().lower()
    code = (referral_code or "").strip().upper()  # codes usually stored uppercase

    if not email or not code:
        return False

    conn = get_conn()  # <-- MUST return a psycopg2 connection

    try:
        with conn:
            with conn.cursor() as cur:
                # 1) Lock NEW USER row
                cur.execute(
                    """
                    SELECT id, COALESCE(referral_bonus_applied, FALSE) AS applied
                    FROM users
                    WHERE lower(email) = %s
                    FOR UPDATE
                    """,
                    (email,),
                )
                nu = cur.fetchone()
                if not nu:
                    return False

                new_user_id, already_applied = nu[0], bool(nu[1])
                if already_applied:
                    return True

                # 2) Lock REFERRER row by referral_code
                cur.execute(
                    """
                    SELECT id, COALESCE(referrals_count, 0) AS cnt
                    FROM users
                    WHERE referral_code = %s
                    FOR UPDATE
                    """,
                    (code,),
                )
                ref = cur.fetchone()
                if not ref:
                    return False

                ref_id, ref_cnt = ref[0], int(ref[1] or 0)

                # 3) Cap enforcement
                if ref_cnt >= REFERRAL_CAP:
                    # Still mark as applied so you don't keep reprocessing
                    cur.execute(
                        """
                        UPDATE users
                        SET referral_bonus_applied = TRUE,
                            referred_by = COALESCE(referred_by, %s)
                        WHERE id = %s
                        """,
                        (code, new_user_id),
                    )
                    return False

                # 4) Pay ONLY the referrer (YOUR REAL CREDIT COLUMNS)
                cur.execute(
                    """
                    UPDATE users
                    SET cv_credits = COALESCE(cv_credits, 0) + %s,
                        ai_credits = COALESCE(ai_credits, 0) + %s,
                        referrals_count = COALESCE(referrals_count, 0) + 1
                    WHERE id = %s
                    """,
                    (BONUS_PER_REFERRAL_CV, BONUS_PER_REFERRAL_AI, ref_id),
                )

                # 5) Mark referral processed on NEW USER
                cur.execute(
                    """
                    UPDATE users
                    SET referral_bonus_applied = TRUE,
                        referred_by = COALESCE(referred_by, %s)
                    WHERE id = %s
                    """,
                    (code, new_user_id),
                )

        return True

    except Exception as e:
        print("apply_referral_bonus error:", e)
        return False



stripe.api_key = os.getenv("STRIPE_SECRET_KEY")  # set in Railway (sk_test_... or sk_live_...)

PRICE_MONTHLY = os.getenv("STRIPE_PRICE_MONTHLY")  # price_...
PRICE_PRO     = os.getenv("STRIPE_PRICE_PRO")      # price_...

APP_URL = os.getenv("APP_URL", "")  # set to https://your-app.up.railway.app
if not APP_URL:
    # safe fallback so app still runs locally
    APP_URL = "http://localhost:8501"

# =========================
# POLICIES: DB HELPERS (psycopg2) - accepted_policies is INTEGER 0/1
# =========================

def has_accepted_policies(email: str) -> bool:
    """
    True if the user accepted policies.
    Uses accepted_policies (0/1) first; falls back to accepted_policies_at.
    Never throws for missing users.
    """
    email = (email or "").strip().lower()
    if not email:
        return False

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(accepted_policies, 0) AS accepted_policies,
                    accepted_policies_at
                FROM users
                WHERE email = %s
                """,
                (email,),
            )
            row = cur.fetchone()
            if not row:
                return False

            # accepted_policies is stored as 0/1 integer
            if int(row.get("accepted_policies") or 0) == 1:
                return True

            return row.get("accepted_policies_at") is not None


def mark_policies_accepted(email: str) -> None:
    """
    Mark policies accepted in DB.
    Sets accepted_policies=1 and stamps accepted_policies_at once.
    """
    email = (email or "").strip().lower()
    if not email:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET
                    accepted_policies = 1,
                    accepted_policies_at = COALESCE(accepted_policies_at, NOW())
                WHERE email = %s
                """,
                (email,),
            )
        conn.commit()





def create_subscription_checkout_session(price_id: str, pack: str, customer_email: str) -> str:
    app_url = (os.getenv("APP_URL") or "http://localhost:8501").rstrip("/")
    success_url = os.getenv("SUCCESS_URL") or f"{app_url}/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url  = os.getenv("CANCEL_URL")  or f"{app_url}/pricing"

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=customer_email,
        line_items=[{"price": price_id, "quantity": 1}],
        allow_promotion_codes=True,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"pack": pack, "app_user_email": customer_email},
    )
    return session.url


def ensure_user_row(email: str) -> int:
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("Missing email")

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (email)
            VALUES (%s)
            ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
            RETURNING id
        """, (email,))
        uid = cur.fetchone()[0]
        conn.commit()
        return uid




from psycopg2.extras import RealDictCursor

def migrate_user_credits_to_ledger_once(email: str) -> None:
    """
    Move legacy users.cv_credits / users.ai_credits into the ledger once.
    Safe to call on every boot/login.
    Skips only if THIS migration has already run for the user.
    """
    email = (email or "").strip().lower()
    if not email:
        return

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1) Get user id + legacy balances (tolerate missing cols by defaulting to 0)
            # If your users table does not have cv_credits/ai_credits, just treat as 0.
            cur.execute(
                """
                SELECT
                    id,
                    COALESCE(cv_credits, 0) AS cv_credits,
                    COALESCE(ai_credits, 0) AS ai_credits
                FROM users
                WHERE email = %s
                """,
                (email,),
            )
            u = cur.fetchone()
            if not u:
                return

            user_id = int(u["id"])
            cv_old = int(u.get("cv_credits") or 0)
            ai_old = int(u.get("ai_credits") or 0)

            # Nothing to migrate
            if cv_old <= 0 and ai_old <= 0:
                return

            # 2) Skip only if the migration grant already exists for this user
            cur.execute(
                """
                SELECT 1
                FROM credit_grants
                WHERE user_id = %s AND source = 'migration_users_columns'
                LIMIT 1
                """,
                (user_id,),
            )
            if cur.fetchone():
                return

            # 3) Write migration grant
            cur.execute(
                """
                INSERT INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at)
                VALUES (%s, 'migration_users_columns', %s, %s, NULL)
                """,
                (user_id, cv_old, ai_old),
            )

            # 4) Zero out legacy columns (prevents double counting / confusion)
            cur.execute(
                """
                UPDATE users
                SET cv_credits = 0,
                    ai_credits = 0
                WHERE id = %s
                """,
                (user_id,),
            )

        conn.commit()




def cooldown_ok(action_key: str, seconds: int = COOLDOWN_SECONDS):
    """
    Per-user cooldown for a given action_key.
    Returns (ok, seconds_left).
    """
    now = time.monotonic()
    last = st.session_state.get(f"_cooldown_{action_key}", 0.0)
    remaining = seconds - (now - last)
    if remaining > 0:
        return False, int(remaining) + 1
    st.session_state[f"_cooldown_{action_key}"] = now
    return True, 0

PRESERVE_KEYS = [
    # personal details
    "full_name", "title", "email", "phone", "location", "summary",
    # job summariser (rename/add your actual keys)
    "job_desc", "job_summary", "job_summary_uses",
    # CV parsed bits
    "_cv_parsed", "_cv_autofill_enabled", "_last_cv_fingerprint",
    # any other section keys you use
]

def snapshot_form_state():
    st.session_state["_form_snapshot"] = {k: st.session_state.get(k) for k in PRESERVE_KEYS}

def restore_form_state():
    snap = st.session_state.get("_form_snapshot") or {}
    for k, v in snap.items():
        if v is not None:
            st.session_state[k] = v


def freeze_defaults():
    # Never overwrite user-entered or AI-generated data
    for k in [
        "skills_text",
        "summary",
        "num_experiences",
        "template_label",
    ]:
        if k in st.session_state and st.session_state[k] is None:
            st.session_state[k] = ""


def backup_skills_state():
    """Keep the last non-empty skills text so reruns can't wipe it to defaults."""
    val = st.session_state.get("skills_text")
    if isinstance(val, str) and val.strip():
        st.session_state["_skills_backup"] = val


def render_auth_modal_if_open():
    if not st.session_state.get("auth_modal_open", False):
        return
    _auth_dialog()  # your existing dialog function


def restore_skills_state():
    """
    Restore skills_text only when it is missing/blank.
    Skips restoring during CV autofill/policy-return transitions to avoid stomping state.
    """

    # If we just autofilled or just returned from policy, don't touch skills this run.
    if st.session_state.get("_skip_restore_skills_once"):
        st.session_state["_skip_restore_skills_once"] = False
        return

    val = st.session_state.get("skills_text")

    # Only restore if empty/None
    if not (val is None or (isinstance(val, str) and not val.strip())):
        return

    # 1) last backup (most recent user/AI version)
    backup = st.session_state.get("_skills_backup")
    if isinstance(backup, str) and backup.strip():
        st.session_state["skills_text"] = backup
        return

    # 2) parsed CV (if available)
    parsed = st.session_state.get("_cv_parsed")
    if isinstance(parsed, dict):
        skills_data = parsed.get("skills")
        if isinstance(skills_data, list) and skills_data:
            merged = ", ".join(str(s).strip() for s in skills_data if str(s).strip())
            if merged.strip():
                st.session_state["skills_text"] = merged
                return
        elif isinstance(skills_data, str) and skills_data.strip():
            st.session_state["skills_text"] = skills_data.strip()
            return

    # If nothing to restore, leave it empty (don't invent defaults)
    st.session_state["skills_text"] = ""



def normalize_skills_state():
    if st.session_state.get("skills_text") is None:
        st.session_state["skills_text"] = ""

    parsed = st.session_state.get("_cv_parsed")
    if isinstance(parsed, dict):
        skills_data = parsed.get("skills")
        if st.session_state.get("skills_text", "").strip() == "":
            if isinstance(skills_data, list) and skills_data:
                st.session_state["skills_text"] = ", ".join(
                    [str(s).strip() for s in skills_data if str(s).strip()]
                )
            elif isinstance(skills_data, str) and skills_data.strip():
                st.session_state["skills_text"] = skills_data.strip()


def tripwire_none_experience_keys():
    keys = ["num_experiences", "job_title_0", "company_0", "description_0"]
    bad = {
        k: st.session_state.get(k)
        for k in keys
        if k in st.session_state and st.session_state.get(k) is None
    }
    if bad:
        st.sidebar.error(f"🚨 Experience keys set to None: {bad}")
        st.sidebar.code("".join(traceback.format_stack(limit=25)))


if st.session_state.get("debug_mode", False):
    tripwire_none_experience_keys()


def _apply_parsed_cv_to_session(parsed: dict, max_edu: int = 5):
    # NOTE: keep your existing personal details / skills / experience mapping above this.

    # -------------------------
    # EDUCATION mapping
    # -------------------------
    edu_list = (
        parsed.get("education")
        or parsed.get("educations")
        or parsed.get("education_history")
        or []
    )

    if not isinstance(edu_list, list):
        edu_list = []

    cleaned = []
    for e in edu_list:
        if not isinstance(e, dict):
            continue

        degree = (e.get("degree") or e.get("qualification") or e.get("title") or "").strip()
        institution = (e.get("institution") or e.get("school") or e.get("university") or "").strip()
        location = (e.get("location") or e.get("city") or "").strip()
        start = (e.get("start_date") or e.get("start") or "").strip()
        end = (e.get("end_date") or e.get("end") or "").strip()

        if degree or institution:
            cleaned.append(
                {
                    "degree": degree,
                    "institution": institution,
                    "location": location,
                    "start": start,
                    "end": end,
                }
            )

    cleaned = cleaned[:max_edu]

    st.session_state["num_education"] = max(1, len(cleaned))

    for i in range(st.session_state["num_education"]):
        row = cleaned[i] if i < len(cleaned) else {}
        st.session_state[f"degree_{i}"] = row.get("degree", "")
        st.session_state[f"institution_{i}"] = row.get("institution", "")
        st.session_state[f"edu_location_{i}"] = row.get("location", "")
        st.session_state[f"edu_start_{i}"] = row.get("start", "")
        st.session_state[f"edu_end_{i}"] = row.get("end", "")

    st.session_state["education_items"] = cleaned


def spend_credits(conn, user_id: int, source: str, cv_amount: int = 0, ai_amount: int = 0) -> bool:
    """
    Spend credits atomically using the SAME connection.
    Do NOT use `with conn:` here (caller may already be using it).
    """
    cv_amount = int(cv_amount or 0)
    ai_amount = int(ai_amount or 0)

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
            if not cur.fetchone():
                conn.rollback()
                return False

            bal = get_user_credits_ledger(conn, user_id)

            if cv_amount > 0 and bal["cv"] < cv_amount:
                conn.rollback()
                return False
            if ai_amount > 0 and bal["ai"] < ai_amount:
                conn.rollback()
                return False

            cur.execute(
                """
                INSERT INTO credit_spends (user_id, source, cv_amount, ai_amount)
                VALUES (%s, %s, %s, %s)
                """,
                (user_id, source, cv_amount, ai_amount),
            )

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        raise



def get_user_credits_ledger(conn, user_id: int) -> dict:
    """
    credits = SUM(grants not expired) - SUM(spends)
    Uses the SAME connection (do not open a new conn here).
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                GREATEST(
                    COALESCE((
                        SELECT SUM(cv_amount)
                        FROM credit_grants
                        WHERE user_id = %s
                          AND (expires_at IS NULL OR expires_at > NOW())
                    ), 0)
                    -
                    COALESCE((
                        SELECT SUM(cv_amount)
                        FROM credit_spends
                        WHERE user_id = %s
                    ), 0),
                0) AS cv,

                GREATEST(
                    COALESCE((
                        SELECT SUM(ai_amount)
                        FROM credit_grants
                        WHERE user_id = %s
                          AND (expires_at IS NULL OR expires_at > NOW())
                    ), 0)
                    -
                    COALESCE((
                        SELECT SUM(ai_amount)
                        FROM credit_spends
                        WHERE user_id = %s
                    ), 0),
                0) AS ai
            """,
            (user_id, user_id, user_id, user_id),
        )
        row = cur.fetchone() or {}
        return {
            "cv": int(row.get("cv", 0) or 0),
            "ai": int(row.get("ai", 0) or 0),
        }


def get_user_credits(email: str) -> dict:
    """
    Public: call this everywhere in the app.
    Looks up user_id by email then returns ledger credits.
    Does NOT create users.
    """
    email = (email or "").strip().lower()
    if not email:
        return {"cv": 0, "ai": 0}

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            u = cur.fetchone()
            if not u:
                return {"cv": 0, "ai": 0}

            user_id = int(u["id"])
            return get_user_credits_ledger(conn, user_id)



def grant_starter_credits_once(user_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Only grant once
            cur.execute(
                "SELECT COALESCE(starter_credits_granted, FALSE) FROM users WHERE id=%s",
                (user_id,),
            )
            already = cur.fetchone()
            if already and already[0]:
                return

            # Grant starter credits via ledger
            cur.execute(
                """
                INSERT INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at, created_at)
                VALUES (%s, 'starter', 5, 5, NULL, NOW())
                """,
                (user_id,),
            )
            cur.execute(
                "UPDATE users SET starter_credits_granted=TRUE WHERE id=%s",
                (user_id,),
            )
            conn.commit()

from psycopg2.extras import RealDictCursor

def spend_ai_credit(email: str, source: str, amount: int = 1) -> bool:
    """
    Spends AI credits from ledger. Returns True if spent, False if insufficient.
    Safe to call from any UI block.
    """
    email = (email or "").strip().lower()
    if not email:
        return False

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE email=%s", (email,))
            row = cur.fetchone()
            if not row:
                return False
            uid = int(row["id"])

        return spend_credits(conn, uid, source=source, ai_amount=int(amount))


def spend_ai(email: str, source: str, amount: int = 1) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE email=%s", (email,))
            row = cur.fetchone()
            if not row:
                return False
            uid = int(row["id"])

        return spend_credits(conn, uid, source=source, ai_amount=amount)

def _clear_education_persistence_for_new_cv():
    """
    Clear education persistence so a new CV upload doesn't get overwritten by old backups.
    """
    for k in list(st.session_state.keys()):
        if k.startswith("degree_") or k.startswith("institution_") or k.startswith("edu_"):
            st.session_state.pop(k, None)

    st.session_state.pop("num_education", None)
    st.session_state.pop("education_items", None)
    st.session_state.pop("_edu_backup", None)


def restore_experience_from_parsed():
    """Restore experience fields from last parsed CV if they went blank after reruns."""
    if not st.session_state.get("_cv_autofill_enabled"):
        return

    parsed = st.session_state.get("_cv_parsed")
    if not isinstance(parsed, dict):
        return

    exps = parsed.get("experiences") or []
    if not isinstance(exps, list) or not exps:
        return

    count = min(len(exps), 5)

    if st.session_state.get("num_experiences") in (None, 0, ""):
        st.session_state["num_experiences"] = count

    for i in range(count):
        exp = exps[i] or {}

        def _restore(key, value):
            if st.session_state.get(key) in (None, "") and isinstance(value, str) and value.strip():
                st.session_state[key] = value

        _restore(f"job_title_{i}", exp.get("job_title", "") or "")
        _restore(f"company_{i}", exp.get("company", "") or "")
        _restore(f"exp_location_{i}", exp.get("location", "") or "")
        _restore(f"start_date_{i}", exp.get("start_date", "") or "")
        _restore(f"end_date_{i}", exp.get("end_date", "") or "")

        desc = exp.get("description", "") or ""
        if isinstance(desc, list):
            desc = "\n".join([str(x) for x in desc if str(x).strip()])
        _restore(f"description_{i}", desc)


def _reset_outputs_on_new_cv():
    """
    Clears derived/generated outputs when a new CV is uploaded.
    """
    keys_to_clear = [
        "_cv_parsed",
        "_cv_autofill_enabled",
        "generated_cv",
        "generated_cover_letter",
        "generated_summary",
        "suggested_bullets",
        "ats_score",
        "final_pdf_bytes",
        "final_docx_bytes",
        "selected_template",
        "download_ready",
    ]
    for k in keys_to_clear:
        st.session_state.pop(k, None)


def normalize_experience_state(max_roles: int = 5):
    if st.session_state.get("num_experiences") is None:
        st.session_state["num_experiences"] = st.session_state.get("parsed_num_experiences", 1)

    for i in range(max_roles):
        for k in ["job_title", "company", "exp_location", "start_date", "end_date", "description"]:
            key = f"{k}_{i}"
            if key in st.session_state and st.session_state[key] is None:
                st.session_state[key] = ""

        st.session_state.setdefault(f"job_title_{i}", "")
        st.session_state.setdefault(f"company_{i}", "")
        st.session_state.setdefault(f"exp_location_{i}", "")
        st.session_state.setdefault(f"start_date_{i}", "")
        st.session_state.setdefault(f"end_date_{i}", "")
        st.session_state.setdefault(f"description_{i}", "")


# -------------------------
# Word limit helpers
# -------------------------
MAX_PANEL_WORDS = 100
MAX_DOC_WORDS = 300
MAX_LETTER_WORDS = 300


def limit_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def clean_cover_letter_body(text: str) -> str:
    cleaned_lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def enforce_word_limit(text: str, max_words: int, label: str = "") -> str:
    words = text.split()
    if len(words) > max_words:
        st.warning(
            f"{label.capitalize()} is limited to {max_words} words. "
            f"Currently {len(words)}; extra words will be ignored in the download."
        )
        return " ".join(words[:max_words])
    return text


def backup_education_state(max_rows: int = 5):
    edu_rows = []
    for i in range(max_rows):
        row = {
            "degree": (st.session_state.get(f"degree_{i}", "") or "").strip(),
            "institution": (st.session_state.get(f"institution_{i}", "") or "").strip(),
            "location": (st.session_state.get(f"edu_location_{i}", "") or "").strip(),
            "start": (st.session_state.get(f"edu_start_{i}", "") or "").strip(),
            "end": (st.session_state.get(f"edu_end_{i}", "") or "").strip(),
        }
        if any(row.values()):
            edu_rows.append(row)

    if edu_rows:
        st.session_state["_edu_backup"] = edu_rows


def restore_education_state(max_rows: int = 5):
    if st.session_state.pop("_skip_restore_education_once", False):
        return

    backup = st.session_state.get("_edu_backup")
    if not isinstance(backup, list) or not backup:
        return

    for i in range(max_rows):
        if (st.session_state.get(f"degree_{i}", "") or "").strip() or (st.session_state.get(f"institution_{i}", "") or "").strip():
            return

    for i, row in enumerate(backup[:max_rows]):
        st.session_state[f"degree_{i}"] = row.get("degree", "")
        st.session_state[f"institution_{i}"] = row.get("institution", "")
        st.session_state[f"edu_location_{i}"] = row.get("location", "")
        st.session_state[f"edu_start_{i}"] = row.get("start", "")
        st.session_state[f"edu_end_{i}"] = row.get("end", "")

# -------------------------
# Resend helper (kept local)
# -------------------------
RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or "").strip()


def _send_password_reset_email_local_DO_NOT_USE(email: str, token: str) -> None:
    key = (os.getenv("RESEND_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("RESEND_API_KEY is missing in environment variables.")

    from_email = (os.getenv("FROM_EMAIL") or "").strip() or "onboarding@resend.dev"
    app_url = (os.getenv("APP_URL") or "").strip()

    if app_url:
        reset_link = f"{app_url.rstrip('/')}/?reset_token={token}"
        body_html = f"""
        <p>Click to reset your password:</p>
        <p><a href="{reset_link}">{reset_link}</a></p>
        <p>If the link doesn’t work, use this reset token: <b>{token}</b></p>
        """
    else:
        body_html = f"""
        <p>Your password reset token is:</p>
        <p><b>{token}</b></p>
        """

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    payload = {
        "from": from_email,
        "to": [email],
        "subject": "Reset your password",
        "html": body_html,
    }

    r = requests.post(
        "https://api.resend.com/emails",
        headers=headers,
        json=payload,
        timeout=20,
    )

    if r.status_code >= 400:
        raise RuntimeError(f"Resend failed: {r.status_code} {r.text}")


def _get_openai_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        if "OPENAI_API_KEY" in st.secrets:
            api_key = st.secrets["OPENAI_API_KEY"]
        elif "OPENAI_KEY" in st.secrets:
            api_key = st.secrets["OPENAI_KEY"]
        elif "openai_api_key" in st.secrets:
            api_key = st.secrets["openai_api_key"]
        elif "openai_key" in st.secrets:
            api_key = st.secrets["openai_key"]
        else:
            for value in st.secrets.values():
                if isinstance(value, str) and value.startswith("sk-"):
                    api_key = value
                    break

    if not api_key:
        raise RuntimeError(
            "No OpenAI API key found. Set OPENAI_API_KEY env var or add it to Streamlit secrets."
        )

    return OpenAI(api_key=api_key)


def _is_logged_in_user(u) -> bool:
    return bool(u and isinstance(u, dict) and u.get("email"))


def _read_uploaded_cv_to_text(uploaded_cv) -> str:
    if uploaded_cv is None:
        return ""

    name = (uploaded_cv.name or "").lower()
    ext = os.path.splitext(name)[1]
    data: bytes = uploaded_cv.getvalue() if hasattr(uploaded_cv, "getvalue") else uploaded_cv.read()

    if not data:
        return ""

    if ext == ".txt":
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1", errors="ignore")

    if ext == ".docx":
        try:
            import docx
        except ImportError:
            raise RuntimeError("Missing dependency: python-docx (pip install python-docx)")

        doc = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text)

    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("Missing dependency: pypdf (pip install pypdf)")

        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            txt = page.extract_text() or ""
            if txt.strip():
                parts.append(txt)
        return "\n\n".join(parts)

    return ""


def locked_action_button(
    label: str,
    *,
    key: str,
    feature_label: str = "This feature",
    counter_key: str | None = None,
    cost: int = 1,
    require_login: bool = True,
    default_tab: str = "Sign in",
    cooldown_name: str | None = None,
    cooldown_seconds: int = 5,
    disabled: bool = False,
    **_ignore,
) -> bool:
    user = st.session_state.get("user")
    is_logged_in = _is_logged_in_user(user)

    clicked = st.button(label, key=key, disabled=disabled)
    if not clicked:
        return False

    if require_login and not is_logged_in:
        st.warning("Sign in to unlock this feature.")
        open_auth_modal(default_tab)
        st.stop()

    if cooldown_name:
        ok, left = cooldown_ok(cooldown_name, cooldown_seconds)
        if not ok:
            st.warning(f"⏳ Please wait {left}s before trying again.")
            st.stop()

    if counter_key:
        if not has_free_quota(counter_key, cost, feature_label):
            st.stop()

    return True


def increment_usage(email: str, counter_key: str, cost: int = 1) -> None:
    cost = int(cost)

    # Decide which credit bucket to burn
    if counter_key in CV_USAGE_KEYS:
        bucket_col = "cv_credits"
    else:
        bucket_col = "ai_credits"

    with get_conn() as conn:
        cur = conn.cursor()

        # Increment usage counter AND decrement credits atomically
        cur.execute(
            f"""
            UPDATE users
            SET
                {counter_key} = COALESCE({counter_key}, 0) + %s,
                {bucket_col} = GREATEST(COALESCE({bucket_col}, 0) - %s, 0)
            WHERE email = %s
            """,
            (cost, cost, email),
        )

        conn.commit()





def get_ai_credits(email: str) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(ai_credits, 0) FROM users WHERE email=%s", (email,))
        row = cur.fetchone()
        return int(row[0]) if row else 0

# =========================
# 2) DB HELPERS (psycopg2)
# Put this in your db.py (or near your existing DB helpers)
# =========================



DATABASE_URL = os.getenv("DATABASE_URL")




def has_stripe_event(stripe_event_id: str) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM stripe_events WHERE stripe_event_id=%s", (stripe_event_id,))
        return cur.fetchone() is not None

def record_stripe_event(stripe_event_id: str, typ: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO stripe_events (stripe_event_id, type) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (stripe_event_id, typ),
        )

def create_credit_grant(
    user_id: int,
    cv_amount: int = 0,
    ai_amount: int = 0,
    source: str = "manual",
    expires_in_days: int | None = 60,
    stripe_event_id: str | None = None,
):
    cv_amount = int(cv_amount or 0)
    ai_amount = int(ai_amount or 0)

    expires_sql = None
    if expires_in_days is not None:
        expires_sql = f"now() + interval '{int(expires_in_days)} days'"

    with get_conn() as conn, conn.cursor() as cur:
        # optional idempotency: don’t double grant for same stripe event
        if stripe_event_id:
            cur.execute("SELECT 1 FROM credit_grants WHERE stripe_event_id=%s", (stripe_event_id,))
            if cur.fetchone():
                return

        if expires_sql:
            cur.execute(
                f"""
                INSERT INTO credit_grants (user_id, source, stripe_event_id, cv_amount, ai_amount, expires_at)
                VALUES (%s, %s, %s, %s, %s, {expires_sql})
                """,
                (user_id, source, stripe_event_id, cv_amount, ai_amount),
            )
        else:
            cur.execute(
                """
                INSERT INTO credit_grants (user_id, source, stripe_event_id, cv_amount, ai_amount, expires_at)
                VALUES (%s, %s, %s, %s, %s, NULL)
                """,
                (user_id, source, stripe_event_id, cv_amount, ai_amount),
            )

# =========================
# CREDITS LEDGER (grants/spends) + SUBSCRIPTIONS + REPAIRS
# =========================
def ensure_credit_tables(conn) -> None:
    """
    Creates / repairs credit_grants, credit_spends, subscriptions tables.
    Safe to run on every boot.
    """
    with conn.cursor() as cur:
        # -------------------------
        # credit_grants
        # -------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS credit_grants (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            cv_amount INTEGER NOT NULL DEFAULT 0,
            ai_amount INTEGER NOT NULL DEFAULT 0,
            expires_at TIMESTAMP NULL,
            created_at TIMESTAMP NOT NULL DEFAULT now()
        );
        """)

        # Add missing columns (safe)
        cur.execute("ALTER TABLE credit_grants ADD COLUMN IF NOT EXISTS user_id INTEGER;")
        cur.execute("ALTER TABLE credit_grants ADD COLUMN IF NOT EXISTS source TEXT;")
        cur.execute("ALTER TABLE credit_grants ADD COLUMN IF NOT EXISTS cv_amount INTEGER NOT NULL DEFAULT 0;")
        cur.execute("ALTER TABLE credit_grants ADD COLUMN IF NOT EXISTS ai_amount INTEGER NOT NULL DEFAULT 0;")
        cur.execute("ALTER TABLE credit_grants ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP NULL;")
        cur.execute("ALTER TABLE credit_grants ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT now();")

        # Repair: if Railway UI created id without sequence/default, force it
        cur.execute("CREATE SEQUENCE IF NOT EXISTS credit_grants_id_seq;")
        cur.execute("""
            ALTER TABLE credit_grants
            ALTER COLUMN id SET DEFAULT nextval('credit_grants_id_seq');
        """)
        cur.execute("""
            SELECT setval(
                'credit_grants_id_seq',
                COALESCE((SELECT MAX(id) FROM credit_grants), 0) + 1,
                false
            );
        """)

        # -------------------------
        # credit_spends
        # -------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS credit_spends (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            cv_amount INTEGER NOT NULL DEFAULT 0,
            ai_amount INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT now()
        );
        """)

        cur.execute("ALTER TABLE credit_spends ADD COLUMN IF NOT EXISTS user_id INTEGER;")
        cur.execute("ALTER TABLE credit_spends ADD COLUMN IF NOT EXISTS source TEXT;")
        cur.execute("ALTER TABLE credit_spends ADD COLUMN IF NOT EXISTS cv_amount INTEGER NOT NULL DEFAULT 0;")
        cur.execute("ALTER TABLE credit_spends ADD COLUMN IF NOT EXISTS ai_amount INTEGER NOT NULL DEFAULT 0;")
        cur.execute("ALTER TABLE credit_spends ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT now();")

        cur.execute("CREATE SEQUENCE IF NOT EXISTS credit_spends_id_seq;")
        cur.execute("""
            ALTER TABLE credit_spends
            ALTER COLUMN id SET DEFAULT nextval('credit_spends_id_seq');
        """)
        cur.execute("""
            SELECT setval(
                'credit_spends_id_seq',
                COALESCE((SELECT MAX(id) FROM credit_spends), 0) + 1,
                false
            );
        """)

        # -------------------------
        # subscriptions
        # -------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            stripe_customer_id TEXT NULL,
            stripe_subscription_id TEXT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            status TEXT NOT NULL DEFAULT 'inactive',
            current_period_end TIMESTAMP NULL,
            cancel_at_period_end BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            updated_at TIMESTAMP NOT NULL DEFAULT now()
        );
        """)

        cur.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS user_id INTEGER;")
        cur.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT NULL;")
        cur.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT NULL;")
        cur.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free';")
        cur.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'inactive';")
        cur.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS current_period_end TIMESTAMP NULL;")
        cur.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN NOT NULL DEFAULT false;")
        cur.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT now();")
        cur.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT now();")

    conn.commit()




# =========================
# 4) STREAMLIT: show "Pro active until X" + ledger credits
# Replace your sidebar "get_user_credits(email)" calls with this pattern
# =========================

def get_active_subscription_for_user(user_id: int):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT plan, status, current_period_end, cancel_at_period_end
                FROM subscriptions
                WHERE user_id = %s
                  AND status IN ('active', 'trialing')
                ORDER BY current_period_end DESC NULLS LAST
                LIMIT 1
                """,
                (user_id,),
            )
            return cur.fetchone()


def _as_utc_dt(ts):
    if not ts:
        return None
    if getattr(ts, "tzinfo", None) is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def format_dt(ts) -> str:
    ts = _as_utc_dt(ts)
    if not ts:
        return ""
    return ts.strftime("%d %b %Y")


session_user = st.session_state.get("user") or {}
email = session_user.get("email")

if email:
    uid = get_user_id(email)
    if uid:
        credits = get_user_credits(email)
        sub = get_active_subscription_for_user(uid)

        now_utc = datetime.now(timezone.utc)

        # machine-safe plan value (DO NOT put formatted strings in here)
        plan_code = (session_user.get("plan") or "free").strip().lower()
        plan_display = plan_code

        if sub:
            period_end = _as_utc_dt(sub.get("current_period_end"))
            sub_plan = (sub.get("plan") or plan_code or "free").strip().lower()

            if period_end and period_end > now_utc:
                plan_code = sub_plan
                plan_display = f"{sub_plan} (active until {format_dt(period_end)})"
            else:
                plan_display = plan_code

        st.session_state["user"]["plan"] = plan_code
        st.session_state["user"]["plan_display"] = plan_display

        cv_left = int(credits.get("cv", 0) or 0)
        ai_left = int(credits.get("ai", 0) or 0)

session_user = st.session_state.get("user") or {}
email = session_user.get("email")

# -------------------------
# GLOBAL THEME + LAYOUT CSS
# (NO st.set_page_config() here — keep that at the top of the file only)
# -------------------------
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

:root{
  --bg: #0b0f19;
  --panel: rgba(255,255,255,0.06);
  --border: rgba(255,255,255,0.12);
  --text: rgba(255,255,255,0.92);
  --muted: rgba(255,255,255,0.70);

  --red: #ff2d55;
  --red2: #ff3b30;

  --radius: 16px;
  --radius-sm: 12px;
  --shadow: 0 18px 50px rgba(0,0,0,0.35);
  --shadow-soft: 0 10px 30px rgba(0,0,0,0.25);
}

/* ---------- Background + base font ---------- */
html, body, [data-testid="stAppViewContainer"]{
  background:
    radial-gradient(1200px 600px at 15% 10%, rgba(255,45,85,0.18), transparent 55%),
    radial-gradient(900px 500px at 80% 15%, rgba(255,59,48,0.12), transparent 55%),
    linear-gradient(180deg, #070a12 0%, var(--bg) 100%) !important;
  color: var(--text) !important;
  font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif !important;
}

/* Remove Streamlit top chrome */
header[data-testid="stHeader"]{ background: transparent !important; }

/* Premium “air” around main */
[data-testid="stAppViewContainer"] .main{
  padding-left: clamp(16px, 4vw, 64px) !important;
  padding-right: clamp(16px, 4vw, 64px) !important;
}

/* Main width / centering */
[data-testid="stAppViewContainer"] .main .block-container{
  padding-top: 2rem !important;
  padding-bottom: 3rem !important;
  max-width: 980px !important;
  margin-left: auto !important;
  margin-right: auto !important;
  padding-left: 2rem !important;
  padding-right: 2rem !important;
}

/* Typography */
h1,h2,h3,h4,h5,h6{ color: var(--text) !important; letter-spacing: -0.02em; }
h1{ font-weight: 800 !important; }
h2{ font-weight: 750 !important; }
p, li { color: var(--text) !important; }
.stCaption, [data-testid="stCaptionContainer"]{ color: var(--muted) !important; }

/* Safer markdown styling */
[data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"]{
  color: var(--text) !important;
}
[data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] p,
[data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] li,
[data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] h1,
[data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] h2,
[data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] h3{
  color: var(--text) !important;
}

/* Sidebar */
[data-testid="stSidebar"]{
  background: #0b0f19 !important;
  border-right: 1px solid rgba(255,255,255,0.10) !important;
  width: 280px !important;
  box-shadow:
    inset -1px 0 0 rgba(255,255,255,0.06),
    8px 0 30px rgba(255,45,85,0.10) !important;
  position: relative;
}

/* Red glow divider */
[data-testid="stSidebar"]::after{
  content: "";
  position: absolute;
  top: 0;
  right: -1px;
  width: 2px;
  height: 100%;
  background: linear-gradient(
    180deg,
    rgba(255,45,85,0.55),
    rgba(255,45,85,0.15),
    rgba(255,45,85,0.55)
  );
  box-shadow: 0 0 18px rgba(255,45,85,0.35),
              0 0 40px rgba(255,45,85,0.18);
  pointer-events: none;
}

section[data-testid="stSidebarNav"]{ display:none !important; }

/* Ensure sidebar text visible */
section[data-testid="stSidebar"] *{
  color: rgba(255,255,255,0.96) !important;
  opacity: 1 !important;
  filter: none !important;
}

/* Expanders */
[data-testid="stExpander"]{
  background: var(--panel) !important;
  border: 1px solid var(--border) !important;
  border-radius: var(--radius) !important;
  box-shadow: var(--shadow-soft) !important;
  overflow: hidden !important;
}

/* Buttons */
.stButton button,
[data-testid="stDownloadButton"] button{
  border-radius: 999px !important;
  border: 1px solid rgba(255,255,255,0.14) !important;
  padding: 0.62rem 1.05rem !important;
  font-weight: 800 !important;
  background: rgba(255,255,255,0.06) !important;
  color: rgba(255,255,255,0.92) !important;
  box-shadow: var(--shadow-soft) !important;
}
.stButton button:hover,
[data-testid="stDownloadButton"] button:hover{
  border-color: rgba(255,45,85,0.55) !important;
  box-shadow: 0 0 0 4px rgba(255,45,85,0.10), var(--shadow) !important;
  transform: translateY(-1px) !important;
}

/* File uploader styling + text visibility */
[data-testid="stFileUploader"] section{
  background: rgba(255,255,255,0.06) !important;
  border: 1px solid rgba(255,255,255,0.14) !important;
  border-radius: 16px !important;
  box-shadow: var(--shadow-soft) !important;
}
[data-testid="stFileUploader"] *{
  color: rgba(255,255,255,0.86) !important;
}
[data-testid="stFileUploader"] button{
  background: linear-gradient(90deg, var(--red) 0%, var(--red2) 100%) !important;
  border: 1px solid rgba(255,45,85,0.55) !important;
  color: #fff !important;
  border-radius: 999px !important;
  font-weight: 900 !important;
  padding: 0.55rem 1.05rem !important;
  box-shadow: 0 12px 35px rgba(255,45,85,0.22) !important;
}

/* ============================
   RIGHT MARKETING RAIL (FIXED)
   ============================ */
@media (min-width: 1200px){
  [data-testid="stAppViewContainer"] .main .block-container{
    padding-right: 420px !important;
  }
}
#mulyba-rail{
  position: fixed;
  top: 140px;
  right: 22px;
  width: 330px;
  z-index: 9999;
}
#mulyba-rail .rail-card{
  background: rgba(255,255,255,0.06);
  border: 1px solid rgba(255,255,255,0.12);
  border-radius: 18px;
  padding: 14px 14px;
  box-shadow: var(--shadow) !important;
  margin-bottom: 12px;
}
#mulyba-rail .rail-title{
  font-weight: 900;
  font-size: 14px;
  margin-bottom: 8px;
  color: rgba(255,255,255,0.95);
}
#mulyba-rail .rail-text{
  font-size: 13px;
  line-height: 1.55;
  color: rgba(255,255,255,0.80);
}
#mulyba-rail .rail-list{
  margin: 0;
  padding-left: 18px;
  color: rgba(255,255,255,0.84);
  line-height: 1.55;
  font-size: 13px;
}
#mulyba-rail .rail-badge{
  display: inline-block;
  padding: 6px 10px;
  border-radius: 999px;
  background: rgba(255,255,255,0.06);
  border: 1px solid rgba(255,255,255,0.12);
  color: rgba(255,255,255,0.90);
  font-weight: 800;
  font-size: 12px;
  margin: 6px 6px 0 0;
}
@media (max-width: 1199px){
  #mulyba-rail{ display:none !important; }
  [data-testid="stAppViewContainer"] .main .block-container{
    padding-right: 2rem !important;
  }
}
</style>
    """,
    unsafe_allow_html=True,
)

# Rail HTML (unchanged)
st.markdown(
    """
<div id="mulyba-rail">
  <div class="rail-card">
    <div class="rail-title">What you get</div>
    <ul class="rail-list">
      <li>Modern CV builder (UK-friendly)</li>
      <li>AI improvements (summary, bullets)</li>
      <li>Cover letters tailored to job ads</li>
      <li>PDF + Word downloads</li>
    </ul>
    <div style="margin-top:10px;">
      <span class="rail-badge">Fast</span>
      <span class="rail-badge">Clean</span>
      <span class="rail-badge">ATS-friendly</span>
    </div>
  </div>

  <div class="rail-card">
    <div class="rail-title">How it works</div>
    <div class="rail-text">
      1) Fill your details<br/>
      2) Improve wording with AI<br/>
      3) Generate & download PDF + Word
    </div>
  </div>

  <div class="rail-card">
    <div class="rail-title">Upgrade when ready</div>
    <div class="rail-text">
      Guests can build. Sign in only when you want downloads + saved history.
    </div>
  </div>
</div>
    """,
    unsafe_allow_html=True,
)

# -------------------------
# INPUT VISIBILITY (WHITE INPUTS + DARK TEXT) — MAIN APP
# -------------------------
st.markdown(
    """
<style>
/* Inputs + textareas */
[data-testid="stAppViewContainer"] input,
[data-testid="stAppViewContainer"] textarea{
  background: rgba(255,255,255,0.96) !important;
  color: #0b0f19 !important;
  -webkit-text-fill-color: #0b0f19 !important;
  caret-color: #ff2d55 !important;
  border: 1px solid rgba(0,0,0,0.10) !important;
}

/* BaseWeb wrappers */
[data-testid="stAppViewContainer"] div[data-baseweb="input"] input,
[data-testid="stAppViewContainer"] div[data-baseweb="textarea"] textarea{
  background: rgba(255,255,255,0.96) !important;
  color: #0b0f19 !important;
  -webkit-text-fill-color: #0b0f19 !important;
  caret-color: #ff2d55 !important;
  border: 1px solid rgba(0,0,0,0.10) !important;
  border-radius: 12px !important;
}

/* Placeholder */
[data-testid="stAppViewContainer"] input::placeholder,
[data-testid="stAppViewContainer"] textarea::placeholder{
  color: rgba(11,15,25,0.45) !important;
  -webkit-text-fill-color: rgba(11,15,25,0.45) !important;
}

/* Autofill */
[data-testid="stAppViewContainer"] input:-webkit-autofill{
  -webkit-text-fill-color: #0b0f19 !important;
  box-shadow: 0 0 0px 1000px rgba(255,255,255,0.96) inset !important;
}
</style>
    """,
    unsafe_allow_html=True,
)

# -------------------------
# AUTH MODAL OVERRIDES (WHITE INPUTS + BLACK TEXT INSIDE MODAL)
# Put this after the general input CSS so it wins inside dialogs.
# -------------------------
st.markdown(
    """
<style>
/* Modal surface */
div[data-baseweb="modal"],
div[data-baseweb="modal"] > div,
div[data-baseweb="modal"] > div > div,
div[role="dialog"],
div[role="dialog"] > div,
div[role="dialog"] section,
div[role="dialog"] header{
  background: rgba(12,14,22,0.92) !important;
}

/* Modal border/shadow */
div[data-baseweb="modal"] > div > div,
div[role="dialog"]{
  border: 1px solid rgba(255,255,255,0.10) !important;
  border-radius: 22px !important;
  box-shadow: 0 30px 120px rgba(0,0,0,0.75) !important;
}

/* Modal text */
div[data-baseweb="modal"] *,
div[role="dialog"] *{
  color: rgba(255,255,255,0.92) !important;
}

/* Modal inputs: WHITE background + BLACK text */
div[role="dialog"] input,
div[role="dialog"] textarea,
div[data-baseweb="modal"] input,
div[data-baseweb="modal"] textarea,
div[aria-modal="true"] input,
div[aria-modal="true"] textarea{
  background: rgba(255,255,255,0.96) !important;
  background-color: rgba(255,255,255,0.96) !important;
  color: #0b0f19 !important;
  -webkit-text-fill-color: #0b0f19 !important;
  caret-color: #ff2d55 !important;
  border: 1px solid rgba(0,0,0,0.10) !important;
  border-radius: 14px !important;
}

/* Modal placeholders */
div[role="dialog"] input::placeholder,
div[role="dialog"] textarea::placeholder{
  color: rgba(11,15,25,0.45) !important;
  -webkit-text-fill-color: rgba(11,15,25,0.45) !important;
}

/* Modal buttons */
div[data-baseweb="modal"] .stButton button,
div[role="dialog"] .stButton button{
  background: linear-gradient(90deg, #ff2d55 0%, #ff3b30 100%) !important;
  color: #fff !important;
  border: 1px solid rgba(255,45,85,0.55) !important;
  border-radius: 999px !important;
  font-weight: 900 !important;
  box-shadow: 0 12px 35px rgba(255,45,85,0.22) !important;
}
</style>
    """,
    unsafe_allow_html=True,
)




# =========================
# AUTH UI
# =========================
def auth_ui():
    """Login / register / password reset UI."""
    import os
    import traceback
    import streamlit as st

    tab_login, tab_register, tab_forgot = st.tabs(
        ["Sign in", "Create account", "Forgot password"]
    )

    # ---- LOGIN TAB ----
    with tab_login:
        login_email = st.text_input("Email", key="auth_login_email")
        login_password = st.text_input(
            "Password", type="password", key="auth_login_password"
        )

        if st.button("Sign in", key="auth_btn_login"):
            if not login_email or not login_password:
                st.error("Please enter both email and password.")
            else:
                user = authenticate_user(login_email, login_password)
                if user:
                    st.session_state["user"] = user

                    # ✅ FORCE consent gate for this user
                    st.session_state["accepted_policies"] = False
                    st.session_state["chk_policy_agree"] = False
                    st.session_state["policy_view"] = None

                    st.session_state["auth_modal_open"] = False
                    st.success(
                        f"Welcome back, {user.get('full_name') or user['email']}!"
                    )
                    st.rerun()
                else:
                    st.error("Invalid email or password.")

    # ---- REGISTER TAB ----
    with tab_register:
        reg_name = st.text_input("Full name", key="auth_reg_name")
        reg_email = st.text_input("Email", key="auth_reg_email")
        reg_password = st.text_input(
            "Password", type="password", key="auth_reg_password"
        )
        reg_password2 = st.text_input(
            "Confirm password", type="password", key="auth_reg_password2"
        )

        reg_referral_code = st.text_input(
            "Referral code (optional)",
            key="auth_reg_referral_code",
            help="If a friend invited you, paste their referral code here.",
        )

        if st.button("Create account", key="auth_btn_register"):
            if not reg_email or not reg_password or not reg_password2:
                st.error("Please fill in all required fields.")
                st.stop()

            if reg_password != reg_password2:
                st.error("Passwords do not match.")
                st.stop()

            referral_code = None

            if reg_referral_code.strip():
                ref_user = get_user_by_referral_code(
                    reg_referral_code.strip()
                )
                if not ref_user:
                    st.error("That referral code is not valid.")
                    st.stop()
                referral_code = reg_referral_code.strip().upper()

            ok = create_user(
                email=reg_email,
                password=reg_password,
                full_name=reg_name,
                referred_by=referral_code,
            )

            if not ok:
                st.error("That email is already registered.")
                st.stop()

            if referral_code:
                apply_referral_bonus(
                    new_user_email=reg_email,
                    referral_code=referral_code,
                )

            user = authenticate_user(reg_email, reg_password)
            if user:
                st.session_state["user"] = user

                # ✅ FORCE consent gate for this user
                st.session_state["accepted_policies"] = False
                st.session_state["chk_policy_agree"] = False
                st.session_state["policy_view"] = None

                st.session_state["auth_modal_open"] = False
                st.rerun()
            else:
                st.success("Account created. Please sign in.")



    # ---- FORGOT PASSWORD TAB ----
    with tab_forgot:
        st.write("If you've forgotten your password, you can reset it here.")

        st.subheader("1. Request a reset link")
        fp_email = st.text_input("Email used for your account", key="auth_fp_email")

        if st.button("Send reset link", key="auth_btn_send_reset"):
            if not fp_email:
                st.error("Please enter your email.")
            else:
                try:
                    # ---- DEBUG START ----
                    print("\n=== RESET EMAIL REQUESTED ===")
                    print("fp_email:", fp_email)

                    resend_key = os.getenv("RESEND_API_KEY", "")
                    from_email = os.getenv("FROM_EMAIL", "")
                    app_url = os.getenv("APP_URL", "")

                    print("RESEND_API_KEY present:", bool(resend_key))
                    print("RESEND_API_KEY length:", len(resend_key))
                    print("RESEND_API_KEY prefix:", resend_key[:3])  # should be 're_'
                    print("FROM_EMAIL present:", bool(from_email))
                    print("APP_URL present:", bool(app_url))
                    # ---- DEBUG END ----

                    token = create_password_reset_token(fp_email)
                    print("token created:", bool(token))

                    if token:
                        send_password_reset_email(fp_email, token)
                        print("send_password_reset_email() finished without raising")

                    st.success("If this email is registered, a reset link has been sent.")

                except Exception as e:
                    print("=== RESET EMAIL ERROR ===")
                    traceback.print_exc()
                    st.error(f"Error while sending reset email: {e}")

        st.markdown("---")
        st.subheader("2. Set a new password using your reset token")

        fp_token = st.text_input("Reset token (from the email)", key="auth_fp_token")
        fp_new_pwd = st.text_input("New password", type="password", key="auth_fp_new_pwd")
        fp_new_pwd2 = st.text_input("Confirm new password", type="password", key="auth_fp_new_pwd2")

        if st.button("Set new password", key="auth_btn_do_reset"):
            if not fp_token or not fp_new_pwd or not fp_new_pwd2:
                st.error("Please fill in all fields.")
            elif fp_new_pwd != fp_new_pwd2:
                st.error("Passwords do not match.")
            else:
                ok = reset_password_with_token(fp_token, fp_new_pwd)
                if ok:
                    st.success(
                        "Password reset successfully. You can now sign in with your new password."
                    )
                else:
                    st.error(
                        "Invalid or expired reset token. Please request a new reset link."
                    )



# =========================
# INIT (run once, early)
# =========================
init_db()
verify_postgres_connection()

st.session_state.setdefault("user", None)
st.session_state.setdefault("accepted_policies", False)
st.session_state.setdefault("policy_view", None)  # None | cookies | privacy | terms | accessibility
st.session_state.setdefault("guest_started_builder", False)



# =========================
# POLICY FILE READER
# =========================
def _read_policy_file(rel_path: str) -> str:
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        fp = os.path.join(here, rel_path)
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception:
        pass
    return ""

# =========================
# POLICY PAGE VIEW
# =========================
def show_policy_page() -> bool:
    view = st.session_state.get("policy_view")
    if not view:
        return False

    title_map = {
        "accessibility": "Accessibility",
        "cookies": "Cookie Policy",
        "privacy": "Privacy Policy",
        "terms": "Terms of Use",
    }

    file_map = {
        "accessibility": "policies/accessibility.md",
        "cookies": "policies/cookie_policy.md",
        "privacy": "policies/privacy_policy.md",
        "terms": "policies/terms_of_use.md",
    }

    st.title(title_map.get(view, "Policy"))
    body = _read_policy_file(file_map.get(view, ""))

    if body.strip():
        st.markdown(body)
    else:
        st.info("Policy content not found in this deployment. Add the markdown file under /policies.")

    if st.button("← Back", key="btn_policy_back"):
        st.session_state["policy_view"] = None
        st.session_state["_just_returned_from_policy"] = True

        # restore only if you have a snapshot saved
        try:
            restore_form_state()
        except Exception:
            pass

        st.rerun()

    return True


# =========================
# CONSENT GATE (POST-LOGIN ONLY) - FAIL CLOSED
# =========================
def show_consent_gate() -> None:
    user = st.session_state.get("user")
    if not (isinstance(user, dict) and user.get("email")):
        return

    email = (user.get("email") or "").strip().lower()
    if not email:
        return

    # Always re-check DB as source of truth
    try:
        accepted_in_db = bool(has_accepted_policies(email))
    except Exception as e:
        st.error(f"Policy check failed. Please refresh and try again. ({e})")
        st.stop()

    # Keep session in sync (prevents weird skips)
    st.session_state["accepted_policies"] = accepted_in_db

    if accepted_in_db:
        return

    # ---- UI (unchanged) ----
    st.markdown(
        """
        <div style="
            border-radius: 12px;
            padding: 18px 20px;
            margin-top: 20px;
            background: #111827;
            border: 1px solid #1f2937;
            color: rgba(255,255,255,0.95);
        ">
            <h3 style="margin-top:0;">Before you continue</h3>
            <p style="font-size:14px; line-height:1.5;">
                We use cookies and process your data to run this CV builder,
                improve the service, and keep your account secure.
                Please open and read the following policies:
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Cookie Policy", key="btn_policy_cookies"):
            snapshot_form_state()
            st.session_state["policy_view"] = "cookies"
            st.rerun()
    with c2:
        if st.button("Privacy Policy", key="btn_policy_privacy"):
            snapshot_form_state()
            st.session_state["policy_view"] = "privacy"
            st.rerun()
    with c3:
        if st.button("Terms of Use", key="btn_policy_terms"):
            snapshot_form_state()
            st.session_state["policy_view"] = "terms"
            st.rerun()

    agree = st.checkbox(
        "I agree to the Cookie Policy, Privacy Policy and Terms of Use",
        key="chk_policy_agree",
    )

    if st.button("Accept and continue", key="btn_policy_accept"):
        if not agree:
            st.warning("Please tick the checkbox to accept.")
            st.stop()

        try:
            mark_policies_accepted(email)
        except Exception as e:
            st.error(f"Could not save your acceptance. Please try again. ({e})")
            st.stop()

        # Re-check DB after write (authoritative)
        try:
            st.session_state["accepted_policies"] = bool(has_accepted_policies(email))
        except Exception:
            # If DB read fails after write, still block to be safe
            st.error("Saved acceptance, but could not verify. Please refresh.")
            st.stop()

        st.rerun()

    st.info("Please accept to continue using the site.")
    st.stop()



# =========================
# AUTH MODAL (friendly box) - define ONCE
# =========================
st.session_state.setdefault("auth_modal_open", False)
st.session_state.setdefault("auth_modal_tab", "Sign in")
st.session_state.setdefault("auth_modal_epoch", 0)

def _is_logged_in_user(u) -> bool:
    return bool(u and isinstance(u, dict) and u.get("email"))

def is_logged_in_user() -> bool:
    return _is_logged_in_user(st.session_state.get("user"))

def open_auth_modal(default_tab: str = "Sign in") -> None:
    st.session_state["auth_modal_tab"] = default_tab
    st.session_state["auth_modal_open"] = True
    st.session_state["auth_modal_epoch"] += 1
    st.rerun()

def close_auth_modal() -> None:
    st.session_state["auth_modal_open"] = False
    st.rerun()

def gate_premium(action_label: str = "use this feature", tab: str = "Sign in") -> bool:
    if is_logged_in_user():
        return True
    st.toast(f"🔒 Sign in to {action_label}", icon="🔒")
    open_auth_modal(tab)
    return False

@st.dialog("Welcome back 👋", width="large")
def _auth_dialog() -> None:
    st.markdown(
        """
        <div style="
            background: rgba(255,255,255,0.06);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 16px;
            padding: 14px 16px;
            margin-bottom: 12px;
        ">
          <div style="font-weight:800; font-size:16px; margin-bottom:4px;">
            Sign in to unlock the tools
          </div>
          <div style="opacity:0.85; font-size:13px; line-height:1.5;">
            Create a modern CV, generate tailored cover letters, and summarise job ads in seconds.
            Your data stays private to your account.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    preferred = st.session_state.get("auth_modal_tab", "Sign in")
    st.caption(f"Tip: You selected **{preferred}**")

    # Your real auth renderer
    auth_ui()

    c1, c2 = st.columns([1, 1])
    with c2:
        if st.button("Close", key=f"auth_modal_close_{st.session_state['auth_modal_epoch']}"):
            close_auth_modal()

def render_auth_modal_if_open() -> None:
    if st.session_state.get("auth_modal_open", False):
        _auth_dialog()

# =========================
# PUBLIC HOME (guest header)
# =========================
def render_public_home() -> None:
    st.markdown(
        """
        <div style="
            background: rgba(255,255,255,0.06);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 20px;
            padding: 18px 20px;
            box-shadow: 0 18px 50px rgba(0,0,0,0.35);
            margin-top: 6px;
            margin-bottom: 18px;
        ">
          <div style="font-weight:900; font-size:30px; letter-spacing:-0.02em; line-height:1.1;">
            Mulyba
          </div>
          <div style="opacity:0.86; font-size:13px; margin-top:8px; line-height:1.55;">
            Career Suite • CV Builder • AI tools
          </div>
          <div style="margin-top:10px; font-size:12px; opacity:0.70;">
            Guests can build. Sign in only when you want downloads + saved history.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# =========================
# PAYWALL + QUOTA HELPERS
# =========================
def show_paywall(feature_label: str) -> None:
    st.markdown(
        f"""
        <div style="
            border-radius: 14px;
            padding: 14px 16px;
            margin: 10px 0 6px 0;
            background: rgba(59,130,246,0.12);
            border: 1px solid rgba(59,130,246,0.35);
        ">
            <div style="font-weight:800; margin-bottom:6px;">
                Limit reached for {feature_label}.
            </div>
            <div style="font-size: 13px; opacity:0.85; margin-bottom: 10px;">
                Upgrade your plan or use referrals to unlock more usage.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

def has_free_quota(counter_key: str, cost: int, feature_label: str) -> bool:
    u = st.session_state.get("user") or {}

    # 🔒 Block guests (cost-control requirement)
    if not (isinstance(u, dict) and u.get("email")):
        st.warning(f"Sign in to use {feature_label}.")
        return False

    # 👑 Owner / admin unlimited
    if u.get("role") in {"owner", "admin"}:
        return True

    email = u.get("email")

    # 🔎 Fetch remaining credits
    credits = get_user_credits(email)

    # Decide which bucket this feature uses
    if counter_key in CV_USAGE_KEYS:
        bucket = "cv"
    else:
        bucket = "ai"

    required = int(cost)
    remaining = int(credits.get(bucket, 0))

    if remaining < required:
        show_paywall(feature_label)
        return False

    return True

def decrement_user_credits(email: str, cv_delta: int = 0, ai_delta: int = 0) -> dict:
    cv_delta = int(cv_delta or 0)
    ai_delta = int(ai_delta or 0)

    if cv_delta < 0 or ai_delta < 0:
        raise ValueError("Deltas must be >= 0")

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET
                cv_credits = cv_credits - %s,
                ai_credits = ai_credits - %s
            WHERE email = %s
              AND cv_credits >= %s
              AND ai_credits >= %s
            RETURNING cv_credits, ai_credits
            """,
            (cv_delta, ai_delta, email, cv_delta, ai_delta),
        )
        row = cur.fetchone()
        conn.commit()

    if not row:
        return {"cv": 0, "ai": 0}

    return {"cv": int(row[0]), "ai": int(row[1])}



def decrement_ai_credit(email: str, amount: int = 1) -> bool:
    """
    Atomic decrement. Returns True if decremented, False if insufficient credits.
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET ai_credits = GREATEST(COALESCE(ai_credits, 0) - %s, 0)
            WHERE email = %s
              AND COALESCE(ai_credits, 0) >= %s
            RETURNING ai_credits
            """,
            (amount, email, amount),
        )
        row = cur.fetchone()
        conn.commit()
        return bool(row)



# =========================
# ROUTING (preview-first)
# =========================
if show_policy_page():
    st.stop()

# ---- Policy return guard (MUST be here) ----
just_returned = st.session_state.pop("_just_returned_from_policy", False)


# render modal early (non-blocking)
render_auth_modal_if_open()

current_user = st.session_state.get("user")
is_logged_in = _is_logged_in_user(current_user)

# Guest header (non-blocking)
if not is_logged_in:
    render_public_home()

# Safe guest placeholder for UI (DO NOT treat as logged in)
if not is_logged_in:
    current_user = {
        "full_name": "Guest",
        "email": None,
        "role": "guest",
        "plan": "free",
        "referral_code": None,
        "referrals_count": 0,
        "is_banned": False,
        "accepted_policies": False,
        "accepted_policies_at": None,
    }

# Always define these (kills NameError)
user_email = (current_user or {}).get("email")  # None for guests
is_admin = (current_user or {}).get("role") in {"owner", "admin"}

# Hydrate counters (real user only) else safe defaults
if is_logged_in and isinstance(st.session_state.get("user"), dict):
    real_user = st.session_state["user"]
    for k, default in USAGE_KEYS_DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = real_user.get(k, default)
else:
    for k, default in USAGE_KEYS_DEFAULTS.items():
        st.session_state.setdefault(k, default)

# Consent gate for logged-in users only
show_consent_gate()

email = (st.session_state.get("user") or {}).get("email")
if email:
    uid = get_user_id(email)  # lookup only (no inserts)
    if not uid:
        st.error("No account found for this email. Please sign out and sign in again.")
        st.stop()
    st.session_state["user_id"] = uid




# =========================
# Referral code (ONLY when logged in)
# =========================
my_ref_code = None
my_ref_count = 0

if is_logged_in and user_email:
    my_ref_code = (st.session_state.get("user") or {}).get("referral_code")
    if not my_ref_code:
        my_ref_code = ensure_referral_code(user_email)
        st.session_state["user"]["referral_code"] = my_ref_code

    my_ref_count = int((st.session_state.get("user") or {}).get("referrals_count", 0) or 0)
    my_ref_count = min(my_ref_count, REFERRAL_CAP)

# =========================
# Admin dashboard
# =========================
def render_admin_dashboard() -> None:
    st.title("👨‍💻 Admin Dashboard")

    users = get_all_users() or []
    total_users = len(users)
    total_paid = sum(
        1 for u in users
        if (u.get("plan") or "free") in {"monthly", "pro", "yearly", "one_time", "premium", "enterprise"}
    )
    total_cvs = sum(int(u.get("cv_generations", 0) or 0) for u in users)
    total_ai = sum(
        int(u.get("summary_uses", 0) or 0)
        + int(u.get("cover_uses", 0) or 0)
        + int(u.get("bullets_uses", 0) or 0)
        + int(u.get("job_summary_uses", 0) or 0)
        + int(u.get("upload_parses", 0) or 0)
        for u in users
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total users", total_users)
    c2.metric("Paid users", total_paid)
    c3.metric("CVs generated", total_cvs)
    c4.metric("AI actions used", total_ai)

    st.subheader("User list")
    if users:
        table_rows = []
        for u in users:
            table_rows.append({
                "Email": u.get("email", ""),
                "Name": u.get("full_name") or "",
                "Plan": u.get("plan", "free"),
                "Role": u.get("role", "user"),
                "Banned": "Yes" if u.get("is_banned") else "No",
                "Policies accepted": "Yes" if u.get("accepted_policies") else "No",
                "Accepted at": (u.get("accepted_policies_at") or "")[:19],
                "Created": (u.get("created_at") or "")[:19],
                "CVs": u.get("cv_generations", 0),
                "Summaries": u.get("summary_uses", 0),
                "Covers": u.get("cover_uses", 0),
                "Bullets": u.get("bullets_uses", 0),
                "Job summaries": u.get("job_summary_uses", 0),
                "Uploads": u.get("upload_parses", 0),
                "Referrals": u.get("referrals_count", 0),
                "Referred by": u.get("referred_by") or "",
            })

        st.dataframe(table_rows, use_container_width=True, height=420)

        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=table_rows[0].keys())
        writer.writeheader()
        writer.writerows(table_rows)
        st.download_button(
            "Download users as CSV",
            data=csv_buffer.getvalue(),
            file_name="users.csv",
            mime="text/csv",
        )
    else:
        st.info("No users yet.")
        return

    st.markdown("---")
    st.subheader("Manage user plans & status")

    selected_email = st.selectbox(
        "Select a user",
        [u["email"] for u in users if u.get("email")],
        key="admin_select_user",
    )
    selected_user = next((u for u in users if u.get("email") == selected_email), None)
    if not selected_user:
        return

    role = selected_user.get("role", "user")
    banned = bool(selected_user.get("is_banned"))
    policies_ok = bool(selected_user.get("accepted_policies"))
    accepted_at = (selected_user.get("accepted_policies_at") or "")[:19]

    st.write(
        f"**User:** {selected_user.get('full_name') or selected_email}\n\n"
        f"**Plan:** `{selected_user.get('plan','free')}`  \n"
        f"**Role:** `{role}`  \n"
        f"**Banned:** {'Yes' if banned else 'No'}  \n"
        f"**Policies accepted:** {'Yes' if policies_ok else 'No'}"
        + (f" ({accepted_at})" if policies_ok and accepted_at else "")
    )

    plan_options = ["free", "monthly", "pro", "one_time", "yearly", "premium", "enterprise"]
    current_plan = selected_user.get("plan", "free")
    if current_plan not in plan_options:
        current_plan = "free"
    new_plan = st.selectbox("New plan", plan_options, index=plan_options.index(current_plan), key="admin_new_plan")

    role_options = ["owner", "admin", "helper", "user"]
    if role not in role_options:
        role = "user"
    new_role = st.selectbox("New role", role_options, index=role_options.index(role), key="admin_new_role")

    col_a, col_b, col_c = st.columns(3)

    with col_a:
        if st.button("Update plan", key="btn_update_plan"):
            set_plan(selected_email, new_plan)
            st.success(f"Plan updated to `{new_plan}` for {selected_email}.")
            st.rerun()

    with col_b:
        if st.button("Update role", key="btn_update_role"):
            if new_role == "helper" and role != "helper":
                helper_count = sum(
                    1 for u in users
                    if u.get("role") == "helper" and u.get("email") != selected_email
                )
                if helper_count >= 4:
                    st.error("You already have 4 helpers. Remove one before adding another.")
                    st.stop()
            set_role(selected_email, new_role)
            st.success(f"Role updated to `{new_role}` for {selected_email}.")
            st.rerun()

    with col_c:
        ban_label = "Unban user" if banned else "Ban user"
        if st.button(ban_label, key="btn_toggle_ban"):
            set_banned(selected_email, not banned)
            st.success(f"{'Unbanned' if banned else 'Banned'} {selected_email}.")
            st.rerun()

    st.markdown("---")
    with st.expander("Danger zone: Delete this user", expanded=False):
        st.warning("This permanently deletes the user and their usage data. Export CSV first if needed.")
        if st.button("Delete this user", key="btn_delete_user"):
            delete_user(selected_email)
            st.success(f"User {selected_email} deleted.")
            if (st.session_state.get("user") or {}).get("email") == selected_email:
                st.session_state["user"] = None
            st.rerun()

# =========================
# Mode select (ADMIN ONLY)
# =========================
if is_admin:
    mode = st.sidebar.radio("Mode", ["Use app", "Admin dashboard"], index=0, key="mode_select")
else:
    mode = "Use app"

if mode == "Admin dashboard":
    render_admin_dashboard()
    st.stop()



def render_mulyba_brand_header(is_logged_in: bool):
    st.markdown(
        """
        <div class="sb-card">
            <div style="font-size:20px; font-weight:900;">🏷️ Mulyba</div>
            <div class="sb-muted">Career Suite • CV Builder • AI tools</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not is_logged_in:
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🔐 Sign in", key="brand_signin_btn"):
                open_auth_modal("Sign in")
                st.rerun()
        with c2:
            if st.button("✨ Create", key="brand_create_btn"):
                open_auth_modal("Create account")
                st.rerun()


# =========================
# SIDEBAR (full)
# =========================
with st.sidebar:
    session_user = st.session_state.get("user")
    sidebar_logged_in = _is_logged_in_user(session_user)

    # ✅ Refresh session user from DB so plan/premium updates instantly
    if sidebar_logged_in:
        email0 = ((session_user or {}).get("email") or "").strip().lower()
        fresh = get_user_by_email(email0)  # dict | None
        if fresh:
            st.session_state["user"] = {**(st.session_state.get("user") or {}), **fresh}
            session_user = st.session_state["user"]

    sidebar_role = (session_user or {}).get("role", "user")

    # Brand header (your existing function)
    render_mulyba_brand_header(sidebar_logged_in)

    # Mode badge
    if sidebar_logged_in:
        st.markdown(
            """
            <div class="mode-badge mode-live">
              <span class="dot"></span> Live mode
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <div class="mode-badge mode-guest">
              <span class="dot"></span> Guest mode
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ---------- Account ----------
    st.markdown('<div class="sb-card">', unsafe_allow_html=True)
    st.markdown("### 👤 Account")

    if not sidebar_logged_in:
        st.markdown("**Guest mode**")
        st.markdown(
            '<div class="sb-muted">Sign in above to unlock downloads, AI tools, and saved history.</div>',
            unsafe_allow_html=True,
        )
        st.markdown("**Status:** ✅ Active")
        st.markdown("**Policies accepted:** No")
    else:
        full_name = (session_user or {}).get("full_name") or "Member"
        email = (session_user or {}).get("email") or "—"
        plan = ((session_user or {}).get("plan") or "free").strip().lower()

        # ✅ show premium properly
        plan_label = "Pro" if plan == "pro" else ("Monthly" if plan == "monthly" else "Free")

        st.markdown(f"**{full_name}**")
        st.markdown(f'<div class="sb-muted">{email}</div>', unsafe_allow_html=True)
        st.markdown(f"**Plan:** {plan_label}")

        if sidebar_role in {"owner", "admin"}:
            st.caption(f"Admin: {sidebar_role}")

        is_banned = bool((session_user or {}).get("is_banned"))
        st.markdown(f"**Status:** {'🚫 Banned' if is_banned else '✅ Active'}")

        accepted = bool((session_user or {}).get("accepted_policies"))
        st.markdown(f"**Policies accepted:** {'Yes' if accepted else 'No'}")

        if st.button("Log out", key="sb_logout_btn"):
            st.session_state["user"] = None
            st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)

    # ---------- Usage ----------
    st.markdown('<div class="sb-card">', unsafe_allow_html=True)
    st.markdown("### 📊 Usage")

    if not sidebar_logged_in:
        st.markdown("**CV Remaining:** 0")
        st.progress(0)
        st.markdown("**AI Remaining:** 0")
        st.progress(0)
        st.caption("Sign in to buy credits and unlock downloads + AI tools.")
    else:
        # Always refresh session user so plan updates after webhook + login
        refresh_session_user_from_db()

        session_user = st.session_state.get("user") or {}
        uid = session_user.get("id")
        plan = (session_user.get("plan") or "free").strip().lower()

        # Admin unlimited
        if sidebar_role in {"owner", "admin"}:
            st.markdown("**CV Generations:** ♾️ Unlimited")
            st.markdown("**AI Tools:** ♾️ Unlimited")
        else:
            credits = {"cv": 0, "ai": 0}
            if uid:
                credits = get_credits_by_user_id(int(uid))  # ledger truth

            cv_left = int(credits.get("cv", 0) or 0)
            ai_left = int(credits.get("ai", 0) or 0)

            used_cv_session = int(st.session_state.get("cv_generations", 0) or 0)
            used_ai_session = int(
                (st.session_state.get("summary_uses", 0) or 0)
                + (st.session_state.get("cover_uses", 0) or 0)
                + (st.session_state.get("bullets_uses", 0) or 0)
                + (st.session_state.get("job_summary_uses", 0) or 0)
                + (st.session_state.get("upload_parses", 0) or 0)
            )

            cv_total_session = max(cv_left + used_cv_session, 1)
            ai_total_session = max(ai_left + used_ai_session, 1)

            st.markdown(f"**Plan:** {plan}")
            st.markdown(f"**CV Remaining:** {cv_left}")
            st.progress(cv_left / cv_total_session)

            st.markdown(f"**AI Remaining:** {ai_left}")
            st.progress(ai_left / ai_total_session)

            st.caption("Credits are calculated from the ledger (credit_grants - credit_spends).")

    st.markdown("</div>", unsafe_allow_html=True)





    # ---------- Referrals ----------
    st.markdown('<div class="sb-card">', unsafe_allow_html=True)
    st.markdown("### 🎁 Referrals")

    if not sidebar_logged_in:
        st.markdown(
            '<div class="sb-muted">Sign in to get your referral code.</div>',
            unsafe_allow_html=True,
        )
    else:
        email = (session_user or {}).get("email")

        # Ensure referral code exists
        ref_code = (session_user or {}).get("referral_code")
        if not ref_code and email:
            ref_code = ensure_referral_code(email)
            st.session_state["user"]["referral_code"] = ref_code
            session_user = st.session_state["user"]

        ref_count = int((session_user or {}).get("referrals_count", 0) or 0)
        ref_count = min(ref_count, REFERRAL_CAP)

        st.markdown(f"**Referrals:** {ref_count} / {REFERRAL_CAP}")
        st.caption(
            f"+{BONUS_PER_REFERRAL_CV} CV & +{BONUS_PER_REFERRAL_AI} AI per referral"
        )

        if ref_code:
            st.markdown("**Your referral code:**")
            st.code(ref_code, language="text")
        else:
            st.warning("Referral code not available yet. Refresh or re-login.")

    st.markdown("</div>", unsafe_allow_html=True)



    # ---------- Help ----------
    st.markdown('<div class="sb-card">', unsafe_allow_html=True)
    st.markdown("### 📘 Help")

    help_topic = st.radio(
        "Choose a topic",
        [
            "Quick Start",
            "AI Tools & Usage",
            "Cover Letter Rules",
            "Templates & Downloads",
            "Troubleshooting",
            "Privacy & Refunds",
        ],
        key="help_topic_sidebar",
    )

    HELP_TEXT = {
        "Quick Start": """
### Quick start (recommended order)

1️⃣ **Fill Personal Details**  
Enter your name, contact details, and location.  
These details appear exactly as entered on your CV and cover letter.

2️⃣ **Add Skills**  
List your most relevant skills, one per line.  
Focus on skills recruiters and ATS systems expect.

3️⃣ **Add Experience**  
Add your work history, starting with your most recent role.  
Use concise bullet points highlighting achievements and impact.

4️⃣ **Add Education**  
Include degrees, certifications, or training.  
Dates are optional and can be edited before download.

5️⃣ **Review, Generate & Download**  
Preview carefully before downloading.  
You are responsible for checking spelling, dates, and accuracy.
""",
        "AI Tools & Usage": """
### AI tools & usage

AI can help:
- Improve summaries and wording
- Rewrite experience bullet points
- Generate tailored cover letters
- Parse uploaded CVs into the form

AI output is **assistance only**.  
Always review and edit before final use.

⏳ Please wait while AI is running before clicking again.
""",
        "Cover Letter Rules": """
### Cover letter rules

To generate a cover letter:
- Personal details must be completed
- At least one experience role is recommended
- Adding a job description improves results

Always review and customise cover letters before sending.
""",
        "Templates & Downloads": """
### Templates & downloads

- Templates affect layout and styling only
- Content does not change when switching templates
- You can preview before downloading

Once downloaded, files cannot be edited inside the app.
""",
        "Troubleshooting": """
### Troubleshooting

- Use one browser tab only
- Do not refresh while AI is running
- Wait for AI actions to complete
- Scroll to review all sections before download
""",
        "Privacy & Refunds": """
### Privacy & refunds

- Upload only information you are comfortable sharing
- Files are processed securely
- You are responsible for final content accuracy

⚠️ Payments are non-refundable due to instant digital delivery.
""",
    }

    st.markdown(HELP_TEXT[help_topic])

    st.markdown(
        """
---
📩 **Need help or spotted an issue?**  
Contact **support@affiliateworldcommissions.com**

Please ensure your details are reviewed before downloading.
""",
    )

    st.markdown("</div>", unsafe_allow_html=True)

# =========================
# CV Upload + AI Autofill (ONE block only)
# =========================
st.subheader("Upload an existing CV (optional)")
st.caption("Upload a PDF/DOCX/TXT, then let AI fill the form for you.")


# ============================================================
# POLICY SNAPSHOT / RESTORE (prevents fields vanishing on policy nav)
# ============================================================

FORM_KEYS_TO_SNAPSHOT = [
    # Section 1
    "full_name", "title", "email", "phone", "location", "summary",

    # Skills / Experience / Education
    "skills_text", "num_experiences", "parsed_num_experiences", "num_education",

    # Target Job / Outputs
    "job_description", "job_summary_ai", "cover_letter", "cover_letter_box",

    # Any other things you KNOW you want preserved:
    "template_label", "references",
]

def snapshot_form_state() -> None:
    snap = {}
    for k in FORM_KEYS_TO_SNAPSHOT:
        if k in st.session_state:
            snap[k] = st.session_state.get(k)
    st.session_state["_form_snapshot"] = snap

def restore_form_state() -> None:
    snap = st.session_state.get("_form_snapshot") or {}
    for k, v in snap.items():
        # Only restore if key is missing OR currently empty/None
        cur = st.session_state.get(k, None)
        if cur is None or (isinstance(cur, str) and not cur.strip()):
            st.session_state[k] = v



# ============================================================
# CV Upload + AI Autofill (ONE block only)
# ============================================================

# ============================================================
# CV Upload + AI Autofill (ONE block only)
# ============================================================

def _safe_set(key: str, value):
    if isinstance(value, str):
        value = value.strip()
    if value is not None and (not isinstance(value, str) or value.strip()):
        st.session_state[key] = value

uploaded_cv = st.file_uploader(
    "Upload your current CV (PDF, DOCX or TXT)",
    type=["pdf", "docx", "txt"],
    key="cv_uploader",
)

fill_clicked = locked_action_button(
    "Fill the form from this CV (AI)",
    key="btn_fill_from_cv",
    feature_label="CV upload & parsing",
    counter_key="upload_parses",
    require_login=True,          # 🔒 blocks guests
    default_tab="Sign in",
    cooldown_name="upload_parse",
    cooldown_seconds=5,
)

if uploaded_cv is not None and fill_clicked:
    raw_text = _read_uploaded_cv_to_text(uploaded_cv)
    if not raw_text.strip():
        st.warning("No readable text found in that file.")
        st.stop()

    cv_fp = hashlib.sha256(raw_text.encode("utf-8", errors="ignore")).hexdigest()
    last_fp = st.session_state.get("_last_cv_fingerprint")

    with st.spinner("Reading and analysing your CV..."):
        parsed = extract_cv_data(raw_text)

    if not isinstance(parsed, dict):
        st.error("AI parser returned an unexpected format.")
        st.stop()

    # reset on new CV
    if cv_fp != last_fp:
        _reset_outputs_on_new_cv()
        _clear_education_persistence_for_new_cv()
        st.session_state["_last_cv_fingerprint"] = cv_fp

    # ✅ Apply parsed data (your existing function)
    _apply_parsed_cv_to_session(parsed)

    # ✅ FORCE Personal details keys to match YOUR NEW cv_* widgets
    _safe_set("cv_full_name", parsed.get("full_name") or parsed.get("name"))
    _safe_set("cv_email", parsed.get("email"))
    _safe_set("cv_phone", parsed.get("phone"))
    _safe_set("cv_location", parsed.get("location"))
    _safe_set("cv_title", parsed.get("title") or parsed.get("professional_title") or parsed.get("current_title"))
    _safe_set("cv_summary", parsed.get("summary") or parsed.get("professional_summary"))


    # ✅ Flags so restore/default logic can’t wipe after rerun
    st.session_state["_cv_parsed"] = parsed
    st.session_state["_cv_autofill_enabled"] = True
    st.session_state["_just_autofilled_from_cv"] = True
    st.session_state["_skip_restore_personal_once"] = True  # << important

    # ✅ usage counting (only for logged-in users)
    email_for_usage = (st.session_state.get("user") or {}).get("email")
    if email_for_usage:
        st.session_state["upload_parses"] = st.session_state.get("upload_parses", 0) + 1
        increment_usage(email_for_usage, "upload_parses")
   
    st.success("Form fields updated from your CV. Scroll down to review and edit.")
    st.rerun()



# ============================================================
# RESTORE GUARDS (stop restore funcs from wiping new data)
# ============================================================

# If we just came back from policy, restore snapshot FIRST (then continue)
if st.session_state.pop("_just_returned_from_policy", False):
    restore_form_state()

# If we just autofilled from CV, DO NOT run restore_* that might overwrite fields
just_autofilled = st.session_state.pop("_just_autofilled_from_cv", False)

# Your existing restore skills calls should NOT run when just_autofilled
if not just_autofilled:
    restore_skills_state()

backup_skills_state()


# -------------------------
# 1. Personal details
# -------------------------
st.header("1. Personal details")

cv_full_name = st.text_input("Full name *", key="cv_full_name")
cv_title     = st.text_input("Professional title (e.g. Software Engineer)", key="cv_title")
cv_email     = st.text_input("Email *", key="cv_email")
cv_phone     = st.text_input("Phone", key="cv_phone")
cv_location  = st.text_input("Location (City, Country)", key="cv_location")

# --- Apply staged summary BEFORE widget renders ---
if "cv_summary_pending" in st.session_state:
    st.session_state["cv_summary"] = st.session_state.pop("cv_summary_pending")

cv_summary_text = st.text_area("Professional summary", height=120, key="cv_summary")
st.caption(f"Tip: keep this under {MAX_PANEL_WORDS} words – extra text will be ignored.")

btn_summary = st.button("Improve professional summary (AI)", key="btn_improve_summary")

if btn_summary:
    if not gate_premium("improve your professional summary"):
        st.stop()

    ok, left = cooldown_ok("improve_summary", 5)
    if not ok:
        st.warning(f"⏳ Please wait {left}s before trying again.")
    else:
        if not cv_summary_text.strip():
            st.error("Please write a professional summary first.")
        elif not has_free_quota("summary_uses", 1, "AI professional summary"):
            st.stop()
        else:
            with st.spinner("Improving your professional summary..."):
                try:
                    cv_like = {
                        "full_name": cv_full_name,
                        "current_title": cv_title,
                        "location": cv_location,
                        "existing_summary": cv_summary_text,
                    }

                    instructions = (
                        "Improve this existing professional summary so it is clearer, "
                        "more impactful and suitable for a modern UK CV. Do not invent "
                        "new experience, just polish what is already there."
                    )

                    improved = generate_tailored_summary(cv_like, instructions)
                    improved_limited = enforce_word_limit(
                        improved,
                        MAX_DOC_WORDS,
                        label="Professional summary (AI)",
                    )

                    # stage for next rerun (do not mutate key after widget renders)
                    st.session_state["cv_summary_pending"] = improved_limited

                    st.session_state["summary_uses"] = st.session_state.get("summary_uses", 0) + 1
                    email_for_usage = (st.session_state.get("user") or {}).get("email")
                    if email_for_usage:
                        increment_usage(email_for_usage, "summary_uses")

                    st.success("AI summary applied into your main box.")
                    st.rerun()

                except Exception as e:
                    st.error(f"AI error (summary improvement): {e}")



normalize_skills_state()
st.header("2. Skills")

# -------------------------
# 2. Skills (bullet points only)
# -------------------------

def normalize_skills_to_bullets(text: str) -> str:
    """
    Takes ANY input (sentences, commas, paragraphs, bullets)
    and outputs clean skill bullets:
    • Skill
    • Skill
    """
    if not text:
        return ""

    raw = text.strip()
    if not raw:
        return ""

    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    items: list[str] = []

    def is_sentence(s: str) -> bool:
        return len(s.split()) > 6 or "," in s or "result" in s.lower() or "through" in s.lower()

    # 1) Break input into candidate chunks
    for ln in lines:
        ln = ln.lstrip("•*-–— \t").strip()
        if not ln:
            continue

        # Split comma-heavy lines
        if "," in ln:
            parts = [p.strip() for p in ln.split(",") if p.strip()]
        else:
            parts = [ln]

        for p in parts:
            if is_sentence(p):
                # reduce sentence to skill-like phrases
                words = p.split()
                if len(words) >= 2:
                    items.append(" ".join(words[:3]))
            else:
                items.append(p)

    # 2) Clean + de-dupe
    seen = set()
    clean: list[str] = []
    for it in items:
        it = it.strip().title()
        if it and it.lower() not in seen:
            seen.add(it.lower())
            clean.append(it)

    # 3) Format as bullets
    return "\n".join(f"• {c}" for c in clean)


# ✅ Apply staged AI value BEFORE widget renders
if "skills_pending" in st.session_state:
    st.session_state["skills_text"] = st.session_state.pop("skills_pending")

# ✅ Default only if missing (never overwrite user CV)
if "skills_text" not in st.session_state or st.session_state["skills_text"] is None:
    st.session_state["skills_text"] = (
        "• Marketing Strategy\n"
        "• Brand Management\n"
        "• Customer Engagement"
    )

skills_text = st.text_area(
    "Skills (one per line)",
    key="skills_text",
    help="Use short skill phrases only (1–3 words per line)",
)

btn_skills = st.button("Improve skills (AI)", key="btn_improve_skills")

if btn_skills:
    if not gate_premium("improve your skills"):
        st.stop()

    ok, left = cooldown_ok("improve_skills", 5)
    if not ok:
        st.warning(f"⏳ Please wait {left}s before trying again.")
        st.stop()

    if not skills_text.strip():
        st.warning("Please add some skills first.")
        st.stop()

    # ✅ Spend 1 AI credit (ledger)  <-- NEW FLOW (replaces has_free_quota)
    email_for_usage = (st.session_state.get("user") or {}).get("email")
    if not email_for_usage:
        st.warning("Please sign in to use AI features.")
        st.stop()

    ok_spend = spend_ai_credit(email_for_usage, source="ai_skills_improve", amount=1)
    if not ok_spend:
        st.warning("You don’t have enough AI credits for this action.")
        st.stop()

    with st.spinner("Improving your skills..."):
        try:
            # 🔥 IMPORTANT: this MUST be skills-specific
            improved = improve_skills(skills_text)

            improved_bullets = normalize_skills_to_bullets(improved)

            improved_limited = enforce_word_limit(
                improved_bullets,
                MAX_DOC_WORDS,
                label="Skills (AI)",
            )

            # ✅ Stage for NEXT run
            st.session_state["skills_pending"] = improved_limited

            # ✅ Analytics (keep this, not credits)
            st.session_state["bullets_uses"] = st.session_state.get("bullets_uses", 0) + 1
            increment_usage(email_for_usage, "bullets_uses")

            st.success("AI skills applied.")
            st.rerun()

        except Exception as e:
            st.error(f"AI error (skills improvement): {e}")



# -------------------------
# Build skills list for downstream use
# -------------------------
skills: list[str] = []
raw = (st.session_state.get("skills_text") or "").strip()

for ln in raw.splitlines():
    ln = ln.lstrip("•*-–— \t").strip()
    if not ln:
        continue
    if "," in ln:
        skills.extend([p.strip() for p in ln.split(",") if p.strip()])
    else:
        skills.append(ln)

# De-dupe
_seen = set()
skills = [s for s in skills if not (s.lower() in _seen or _seen.add(s.lower()))]




restore_experience_from_parsed()
st.header("3. Experience (multiple roles)")

# -------------------------
# 3. Experience (multiple roles)
# -------------------------

# ✅ If we just autofilled from CV, sync the UI role count to what was parsed
if st.session_state.get("_just_autofilled_from_cv", False):
    parsed_n = int(st.session_state.get("parsed_num_experiences", 1) or 1)
    st.session_state["num_experiences"] = max(1, min(5, parsed_n))  # respect UI bounds

# Keep count stable (parsed -> UI) (only if still missing/None)
if "num_experiences" not in st.session_state or st.session_state["num_experiences"] is None:
    st.session_state["num_experiences"] = st.session_state.get("parsed_num_experiences", 1)

# used to run AI after render
st.session_state.setdefault("ai_running_role", None)
st.session_state.setdefault("ai_run_now", False)

num_experiences = st.number_input(
    "How many roles do you want to include?",
    min_value=1,
    max_value=5,
    step=1,
    key="num_experiences",
)

experiences = []

# ---- Render roles ----
for i in range(int(num_experiences)):
    st.subheader(f"Role {i + 1}")

    job_title_key = f"job_title_{i}"
    company_key   = f"company_{i}"
    loc_key       = f"exp_location_{i}"
    start_key     = f"start_date_{i}"
    end_key       = f"end_date_{i}"
    desc_key      = f"description_{i}"
    pending_key   = f"description_pending_{i}"

    # ✅ Apply staged AI BEFORE the widget renders
    if pending_key in st.session_state:
        st.session_state[desc_key] = st.session_state.pop(pending_key)

    # ✅ Ensure keys exist (never None)
    if st.session_state.get(job_title_key) is None: st.session_state[job_title_key] = ""
    if st.session_state.get(company_key)   is None: st.session_state[company_key]   = ""
    if st.session_state.get(loc_key)       is None: st.session_state[loc_key]       = ""
    if st.session_state.get(start_key)     is None: st.session_state[start_key]     = ""
    if st.session_state.get(end_key)       is None: st.session_state[end_key]       = ""
    if st.session_state.get(desc_key)      is None: st.session_state[desc_key]      = ""

    # widgets
    job_title = st.text_input("Job title", key=job_title_key)
    company   = st.text_input("Company", key=company_key)
    exp_loc   = st.text_input("Job location", key=loc_key)
    start_dt  = st.text_input("Start date (e.g. Jan 2020)", key=start_key)
    end_dt    = st.text_input("End date (e.g. Present or Jun 2023)", key=end_key)

    desc_value = st.text_area(
        "Description / key achievements",
        key=desc_key,
        help="Use one bullet per line.",
    )

    # ✅ Button only schedules AI (no AI work inside loop)
    btn_role = st.button("Improve this role (AI)", key=f"btn_role_ai_{i}")
    if btn_role:
        if not gate_premium(f"improve Role {i+1} with AI"):
            st.stop()
        ok, left = cooldown_ok(f"improve_role_{i}", 5)
        if not ok:
            st.warning(f"⏳ Please wait {left}s before trying again.")
            st.stop()

        st.session_state["ai_running_role"] = i
        st.session_state["ai_run_now"] = True
        st.rerun()

    # Build Experience objects
    if job_title and company:
        experiences.append(
            Experience(
                job_title=job_title,
                company=company,
                location=exp_loc or None,
                start_date=start_dt or "",
                end_date=end_dt or None,
                description=(st.session_state.get(desc_key) or None),
            )
        )


# ---------- Run AI AFTER the loop (single, correct) ----------
role_to_improve = st.session_state.get("ai_running_role")
run_now = st.session_state.pop("ai_run_now", False)  # pop so it runs once

if run_now and role_to_improve is not None:
    i = int(role_to_improve)

    # IMPORTANT: clear role flag early so reruns don't re-trigger
    st.session_state["ai_running_role"] = None

    if not gate_premium("use AI role improvements"):
        st.stop()

    desc_key     = f"description_{i}"
    pending_key  = f"description_pending_{i}"
    current_text = (st.session_state.get(desc_key) or "").strip()

    if not current_text:
        st.warning("Please add text for this role first.")
        st.stop()

    # ✅ Replace free-quota check with AI credit spend (ledger)
    email_for_usage = (st.session_state.get("user") or {}).get("email")
    if not email_for_usage:
        st.warning("Please sign in to use AI features.")
        st.stop()

    ok_spend = spend_ai_credit(email_for_usage, source=f"ai_role_improve_{i+1}", amount=1)
    if not ok_spend:
        st.warning("You don’t have enough AI credits for this action.")
        st.stop()

    with st.spinner(f"Improving Role {i+1} description..."):
        try:
            improved = improve_bullets(current_text)
            improved_limited = enforce_word_limit(
                improved,
                MAX_DOC_WORDS,
                label=f"Role {i+1} description",
            )

            # Stage update for next render
            st.session_state[pending_key] = improved_limited

            # ✅ Keep existing analytics increment right after success
            st.session_state["bullets_uses"] = st.session_state.get("bullets_uses", 0) + 1
            increment_usage(email_for_usage, "bullets_uses")

            st.success(f"Role {i+1} updated.")
            st.rerun()

        except Exception as e:
            st.error(f"AI error: {e}")


# ✅ Keep your existing pop (ensures sync only happens once after autofill)
if not st.session_state.pop("_just_autofilled_from_cv", False):
    pass


    restore_education_state()

# -------------------------
# 4. Education (multiple entries)
# -------------------------
st.header("4. Education (multiple entries)")

if "num_education" not in st.session_state:
    st.session_state["num_education"] = 1

num_education = st.number_input(
    "How many education entries do you want to include?",
    min_value=1,
    max_value=5,
    step=1,
    key="num_education",
)

education_items = []

for i in range(int(num_education)):
    st.subheader(f"Education {i + 1}")

    # ✅ Blank defaults (no placeholder education)
    default_degree = ""
    default_institution = ""
    default_location = ""
    default_start = ""
    default_end = ""

    degree_key = f"degree_{i}"
    institution_key = f"institution_{i}"
    edu_location_key = f"edu_location_{i}"
    edu_start_key = f"edu_start_{i}"
    edu_end_key = f"edu_end_{i}"

    if degree_key not in st.session_state:
        st.session_state[degree_key] = default_degree
    if institution_key not in st.session_state:
        st.session_state[institution_key] = default_institution
    if edu_location_key not in st.session_state:
        st.session_state[edu_location_key] = default_location
    if edu_start_key not in st.session_state:
        st.session_state[edu_start_key] = default_start
    if edu_end_key not in st.session_state:
        st.session_state[edu_end_key] = default_end

    degree = st.text_input("Degree / qualification", key=degree_key)
    institution = st.text_input("Institution", key=institution_key)
    edu_location = st.text_input("Education location", key=edu_location_key)
    edu_start = st.text_input("Start date (e.g. Sep 2016)", key=edu_start_key)
    edu_end = st.text_input("End date (e.g. Jun 2019)", key=edu_end_key)

    # ✅ Only append real education (prevents empty rows being passed to AI)
    if degree.strip() and institution.strip():
        education_items.append(
            Education(
                degree=degree.strip(),
                institution=institution.strip(),
                location=edu_location.strip() or None,
                start_date=edu_start.strip() or "",
                end_date=edu_end.strip() or None,
            )
        )

# ✅ CRITICAL: save what the user typed so reruns can't wipe it
backup_education_state()
st.session_state["education_items"] = [edu.dict() for edu in education_items]

# -------------------------
# 5. References (optional)
# -------------------------
st.header("6. References (optional)")

if "references" not in st.session_state:
    st.session_state["references"] = ""

references = st.text_area(
    "References (leave blank to omit from CV)",
    key="references",
    help=(
        "Example: 'Available on request' or list names, roles and contact details. "
        "Line breaks will be preserved in the PDF."
    ),
)

# =========================
# Job Search (Adzuna) — Expander + Uses SAME user credits as the rest of your app
# ✅ No extra AI counter UI
# ✅ No st.stop() (won't hide other features)
# ✅ Refresh user from Postgres (get_user_by_email) then read credits from that user object
# =========================

import streamlit as st
from adzuna_client import search_jobs, AdzunaConfigError, AdzunaAPIError

# -------- Helpers --------
@st.cache_data(ttl=300, show_spinner=False)
def _cached_adzuna_search(query: str, location: str, results: int = 10):
    return search_jobs(query=query, location=location, results=results)

def _format_salary(smin, smax) -> str:
    if smin is None and smax is None:
        return ""
    try:
        if smin is not None and smax is not None:
            return f"Salary: £{int(smin):,} - £{int(smax):,}"
        if smin is not None:
            return f"Salary: from £{int(smin):,}"
        return f"Salary: up to £{int(smax):,}"
    except Exception:
        return "Salary: available"

def _extract_ai_credits_from_user(user: dict) -> int | None:
    """
    Pull AI credits from the same user object your app uses.
    Returns None if it can't confidently find it.
    """
    if not isinstance(user, dict):
        return None

    # 1) Common direct keys
    common_keys = [
        "ai_remaining",
        "ai_credits",
        "ai_credit",
        "ai_credits_remaining",
        "aiRemaining",
        "credits_ai",
        "ai",
    ]
    for k in common_keys:
        v = user.get(k)
        if v is not None:
            try:
                return int(v)
            except Exception:
                pass

    # 2) Common nested structures (many apps store usage in a nested dict)
    for nested_key in ("usage", "user_usage", "limits", "credits"):
        nested = user.get(nested_key)
        if isinstance(nested, dict):
            for k in common_keys + ["remaining", "ai_remaining", "ai_credits"]:
                v = nested.get(k)
                if v is not None:
                    try:
                        return int(v)
                    except Exception:
                        pass

    # 3) Heuristic fallback: find an int field with "ai" + ("remain"/"credit") in key name
    candidates = []
    for k, v in user.items():
        if not isinstance(k, str):
            continue
        key_l = k.lower()
        if ("ai" in key_l) and (("remain" in key_l) or ("credit" in key_l)):
            try:
                candidates.append(int(v))
            except Exception:
                pass
    if candidates:
        # if multiple, take the max (usually the remaining balance)
        return max(candidates)

    return None

def _safe_refresh_user_from_db(email: str) -> dict | None:
    """
    Uses your existing helper get_user_by_email(email) to refresh from Postgres.
    If your helper name differs, rename this function call below.
    """
    try:
        fresh = get_user_by_email(email)  # <-- rename if yours differs
        return fresh if isinstance(fresh, dict) else None
    except Exception:
        return None


# -----------------------------
# UI (Expander)
# -----------------------------
expanded = bool(st.session_state.get("adzuna_results"))
with st.expander("🔎 Job Search (Adzuna)", expanded=expanded):

    st.session_state.setdefault("adzuna_results", [])

    # --- AUTH ---
    session_user = st.session_state.get("user") or {}
    email = (session_user.get("email") or "").strip().lower()

    can_use = True
    if not email:
        st.warning("Please sign in to use Job Search.")
        can_use = False

    uid = None
    credits = {"cv": 0, "ai": 0}

    if can_use:
        uid = get_user_id(email)
        if not uid:
            st.warning("Couldn’t find your account. Please sign out and sign in again.")
            can_use = False
        else:
            credits = get_credits_by_user_id(uid)
            if int(credits.get("ai", 0) or 0) <= 0:
                st.warning("You have 0 AI credits. Buy more credits to use Job Search.")
                can_use = False

    # Inputs
    with st.container(border=True):
        col1, col2, col3 = st.columns([3, 3, 1.4])
        with col1:
            keywords = st.text_input(
                "Keywords",
                key="adzuna_keywords",
                placeholder="e.g. marketing manager / software engineer",
                disabled=not can_use,
            )
        with col2:
            location = st.text_input(
                "Location",
                key="adzuna_location",
                placeholder="e.g. Walsall or WS2",
                disabled=not can_use,
            )
        with col3:
            st.write("")
            st.write("")
            search_clicked = st.button(
                "Search",
                type="primary",
                key="adzuna_search_btn",
                use_container_width=True,
                disabled=not can_use,
            )

        st.caption("Tip: leave Location blank to search broadly, or use a postcode for local roles.")

    def _as_text(x):
        if x is None:
            return ""
        if isinstance(x, str):
            return x
        if isinstance(x, dict):
            # common Adzuna shapes
            return (
                x.get("display_name")
                or x.get("name")
                or x.get("area")
                or x.get("label")
                or str(x)
            )
        return str(x)

    def _normalize_jobs(jobs_raw):
        """
        Adzuna wrappers vary. Ensure we end up with: list[dict]
        """
        if jobs_raw is None:
            return []
        if isinstance(jobs_raw, dict):
            # common wrappers
            jobs_raw = jobs_raw.get("results") or jobs_raw.get("data") or jobs_raw.get("jobs") or []
        if not isinstance(jobs_raw, list):
            return []
        # filter to dict items only
        return [j for j in jobs_raw if isinstance(j, dict)]

    if search_clicked and can_use:
        query_clean = (keywords or "").strip()
        loc_clean = (location or "").strip()

        if not query_clean:
            st.info("Enter keywords to search (e.g., “marketing manager”).")
        else:
            try:
                with st.spinner("Searching jobs..."):
                    jobs_raw = _cached_adzuna_search(query_clean, loc_clean, results=10)

                jobs = _normalize_jobs(jobs_raw)

                # ✅ Spend 1 AI credit only if API returned successfully (even if 0 results)
                spent = try_spend(uid, source="job_search", ai=1)
                if not spent:
                    st.warning("You don’t have enough AI credits to perform this search.")
                    st.stop()

                st.session_state["adzuna_results"] = jobs

                if not jobs:
                    st.info("No results found. Try different keywords or a nearby location.")

                st.rerun()

            except AdzunaConfigError:
                st.error("Job search is not configured. Missing Adzuna keys in Railway Variables.")
            except AdzunaAPIError:
                st.error("Job search is temporarily unavailable. Please try again shortly.")
            except Exception as e:
                st.error(f"Job search failed: {e}")

    # -----------------------------
    # Results (each job collapsible)
    # -----------------------------
    jobs = st.session_state.get("adzuna_results") or []
    jobs = _normalize_jobs(jobs)

    if jobs:
        st.divider()
        st.caption(f"Showing up to {min(len(jobs), 10)} results.")

        for idx, job in enumerate(jobs):
            title = _as_text(job.get("title")) or "Untitled"

            # company can be dict or string depending on API
            company_val = job.get("company")
            company = _as_text(company_val) or "Unknown company"

            # location can be dict (display_name) or string
            loc_val = job.get("location") or job.get("candidate_required_location") or job.get("area")
            loc = _as_text(loc_val) or "Unknown location"

            created = _as_text(job.get("created") or job.get("created_at") or "")
            url = _as_text(job.get("redirect_url") or job.get("url") or "")
            smin = job.get("salary_min")
            smax = job.get("salary_max")
            desc = _as_text(job.get("description") or "")

            with st.expander(f"{title} — {company} ({loc})", expanded=(idx == 0)):

                with st.container(border=True):
                    top = st.columns([4, 1])
                    with top[0]:
                        if created:
                            st.caption(f"Posted: {created}")
                        sal = _format_salary(smin, smax)
                        if sal:
                            st.caption(sal)
                        if url:
                            st.link_button("Open listing", url)

                    with top[1]:
                        if st.button("Use this job", key=f"use_job_{idx}", use_container_width=True):
                            # NOTE: Keep job state names job_* so they never collide with cv_*
                            st.session_state["job_description"] = desc
                            st.session_state["_last_jd_fp"] = None
                            st.session_state.pop("job_summary_ai", None)
                            st.session_state.pop("cover_letter", None)
                            st.session_state.pop("cover_letter_box", None)

                            st.session_state["selected_job"] = {
                                "title": title,
                                "company": company,
                                "url": url,
                                "location": loc,
                            }

                            st.success("Job loaded into Target Job. Now generate Summary / Cover Letter.")
                            st.rerun()

                st.markdown("**Preview description**")
                st.write(desc[:2500] + ("..." if len(desc) > 2500 else ""))



# -------------------------
# 5. Target Job (optional, for AI)
# -------------------------
st.header("5. Target Job (optional)")

import hashlib

def _fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8", errors="ignore")).hexdigest()

def get_personal_value(primary_key: str, fallback_key: str) -> str:
    """Read personal details from either the main Section 1 keys OR cv_* keys."""
    return (st.session_state.get(primary_key) or st.session_state.get(fallback_key) or "").strip()

# Pull personal details safely (works with either key system)
full_name_ss = get_personal_value("full_name", "cv_full_name")
email_ss     = get_personal_value("email", "cv_email")
title_ss     = get_personal_value("title", "cv_title")
phone_ss     = get_personal_value("phone", "cv_phone")
location_ss  = get_personal_value("location", "cv_location")

job_description = st.text_area(
    "Paste the job description here",
    height=200,
    help="Paste the full job spec from LinkedIn, Indeed, etc.",
    key="job_description",
)

jd_fp = _fingerprint(job_description)
last_jd_fp = st.session_state.get("_last_jd_fp")

# If JD changed, clear AI outputs
if last_jd_fp and jd_fp != last_jd_fp:
    st.session_state.pop("job_summary_ai", None)
    st.session_state.pop("cover_letter", None)
    st.session_state.pop("cover_letter_box", None)

st.session_state["_last_jd_fp"] = jd_fp

st.caption(
    f"For best results, keep this to {MAX_DOC_WORDS} words or less. "
    "(Extra words are ignored.)"
)

col_jd1, col_jd2 = st.columns(2)
with col_jd1:
    job_summary_clicked = st.button("Suggest tailored summary (AI)", key="btn_job_summary")
with col_jd2:
    ai_cover_letter_clicked = st.button("Generate cover letter (AI)", key="btn_cover")

# -------------------------
# AI job-description summary
# -------------------------
if job_summary_clicked:
    if not gate_premium("generate a job summary"):
        st.stop()

    # ✅ use safe personal values
    if not (full_name_ss and email_ss):
        st.warning("Complete Section 1 (Full name + Email) first — these are used in outputs.")
        st.stop()

    if not job_description.strip():
        st.error("Please paste a job description first.")
        st.stop()

    # ✅ LEDGER SPEND (1 AI credit)
    email_for_usage = (st.session_state.get("user") or {}).get("email") or ""
    uid = get_user_id(email_for_usage) if email_for_usage else None
    if not uid:
        st.error("Please sign in again.")
        st.stop()

    spent = try_spend(uid, source="job_summary", ai=1)
    if not spent:
        st.warning("You don’t have enough AI credits to generate a job summary.")
        st.stop()

    with st.spinner("Generating AI job summary..."):
        try:
            jd_limited = enforce_word_limit(job_description, MAX_DOC_WORDS, label="Job description")
            job_summary_text = generate_job_summary(jd_limited)

            st.session_state["job_summary_ai"] = job_summary_text
            st.session_state["job_summary_uses"] = st.session_state.get("job_summary_uses", 0) + 1

            # Optional analytics only (won't affect credits)
            if email_for_usage:
                increment_usage(email_for_usage, "job_summary_uses")

            st.success("AI job summary generated below.")
        except Exception as e:
            st.error(f"AI error (job summary): {e}")

# Display job summary
job_summary_text = st.session_state.get("job_summary_ai", "")
if job_summary_text:
    st.markdown("**AI job summary for this role (read-only):**")
    st.write(job_summary_text)

# -------------------------
# AI cover letter generation
# -------------------------
if ai_cover_letter_clicked:
    if not gate_premium("generate a cover letter"):
        st.stop()

    # ✅ use safe personal values
    if not (full_name_ss and email_ss):
        st.warning("Complete Section 1 (Full name + Email) first — added to cover letter.")
        st.stop()

    if not job_description.strip():
        st.error("Please paste a job description first.")
        st.stop()

    # ✅ LEDGER SPEND (1 AI credit)
    email_for_usage = (st.session_state.get("user") or {}).get("email") or ""
    uid = get_user_id(email_for_usage) if email_for_usage else None
    if not uid:
        st.error("Please sign in again.")
        st.stop()

    spent = try_spend(uid, source="cover_letter", ai=1)
    if not spent:
        st.warning("You don’t have enough AI credits to generate a cover letter.")
        st.stop()

    with st.spinner("Generating cover letter..."):
        try:
            cover_input = {
                "full_name": full_name_ss,
                "current_title": title_ss,
                "skills": skills,
                "experiences": [exp.dict() for exp in experiences],
                "education": st.session_state.get("education_items", []),
                "location": location_ss,
            }

            jd_limited = enforce_word_limit(job_description, MAX_DOC_WORDS, label="Job description (AI input)")
            job_summary = st.session_state.get("job_summary_ai", "") or ""

            cover_text = generate_cover_letter_ai(cover_input, jd_limited, job_summary)
            cleaned = clean_cover_letter_body(cover_text)
            final_letter = enforce_word_limit(cleaned, MAX_LETTER_WORDS, label="cover letter")

            st.session_state["cover_letter"] = final_letter
            st.session_state["cover_letter_box"] = final_letter

            st.session_state["cover_uses"] = st.session_state.get("cover_uses", 0) + 1
            if email_for_usage:
                increment_usage(email_for_usage, "cover_uses")

            st.success("AI cover letter generated below. You can edit it before downloading.")
            st.rerun()

        except Exception as e:
            st.error(f"AI error (cover letter): {e}")

# -------------------------
# Cover letter editor + downloads
# -------------------------
st.session_state.setdefault("cover_letter", "")

if st.session_state["cover_letter"]:
    st.subheader("✏️ Cover letter")

    edited = st.text_area(
        "You can edit this before using it:",
        key="cover_letter_box",
        height=260,
    )
    st.session_state["cover_letter"] = edited

    try:
        # ✅ use safe values so we never hit NameError or blank fields
        letter_pdf = render_cover_letter_pdf_bytes(
            full_name=full_name_ss or "Candidate",
            letter_body=st.session_state["cover_letter"],
            location=location_ss,
            email=email_ss,
            phone=phone_ss,
        )

        letter_docx = render_cover_letter_docx_bytes(
            full_name=full_name_ss or "Candidate",
            letter_body=st.session_state["cover_letter"],
            location=location_ss,
            email=email_ss,
            phone=phone_ss,
        )

        col_d11, col_d12 = st.columns(2)
        with col_d11:
            st.download_button(
                label="📄 Download cover letter as PDF",
                data=letter_pdf,
                file_name="cover_letter.pdf",
                mime="application/pdf",
            )
        with col_d12:
            st.download_button(
                label="📝 Download cover letter as Word (.docx)",
                data=letter_docx,
                file_name="cover_letter.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

    except Exception as e:
        st.error(f"Error generating cover letter files: {e!r}")





# -------------------------
# CV Template mapping
# -------------------------
TEMPLATE_MAP = {
    "Blue": "Blue Theme.html",
    "Green": "Green Theme.html",
    "Purple": "Purple Theme.html",
    "Red": "Red Theme.html",
    "Elegant": "cv_elegant.html",
    "Classic Grey": "classic_grey.html",
}

# ✅ Ensure a default template label exists
if "template_label" not in st.session_state or not st.session_state["template_label"]:
    st.session_state["template_label"] = "Blue"

# ✅ UI: Template dropdown
template_label = st.selectbox(
    "Choose a CV template",
    options=list(TEMPLATE_MAP.keys()),
    key="template_label",
    index=(
        list(TEMPLATE_MAP.keys()).index(st.session_state["template_label"])
        if st.session_state["template_label"] in TEMPLATE_MAP
        else 0
    ),
)



# -------------------------
# Generate CV (spend 1 credit)
# -------------------------
generate_clicked = locked_action_button(
    "Generate CV (PDF + Word)",
    action_label="generate and download your CV",
    key="btn_generate_cv",
)

if generate_clicked:
    # IMPORTANT: make sure this does NOT clear cv_* keys
    # If it does, comment it out or fix it
    clear_ai_upload_state_only()

    email_for_usage = (st.session_state.get("user") or {}).get("email")

    # Pull CV fields ONLY from cv_* keys
    cv_full_name = get_cv_field("cv_full_name")
    cv_title     = get_cv_field("cv_title")
    cv_email     = get_cv_field("cv_email")
    cv_phone     = get_cv_field("cv_phone")
    cv_location  = get_cv_field("cv_location")
    raw_summary  = get_cv_field("cv_summary", "")

    # Validate CV fields (NOT auth email)
    if not cv_full_name or not cv_email:
        st.error("Please fill in at least your full name and email.")
        st.stop()

    # Validate login
    if not email_for_usage:
        st.error("Please sign in again.")
        open_auth_modal("Sign in")
        st.stop()

    uid = get_user_id(email_for_usage)
    if not uid:
        st.error("Please sign in again.")
        st.stop()

    # Spend ledger credit (1 CV)
    spent = try_spend(uid, source="cv_generate", cv=1)
    if not spent:
        st.warning("You don’t have enough CV credits to generate a CV.")
        st.stop()

    try:
        cv_summary = enforce_word_limit(
            raw_summary or "",
            MAX_DOC_WORDS,
            "Professional summary",
        )

        cv = CV(
            full_name=cv_full_name,
            title=cv_title or None,
            email=cv_email,
            phone=cv_phone or None,
            full_address=None,
            location=cv_location or None,
            summary=cv_summary or None,
            skills=skills,
            experiences=experiences,
            education=education_items,
            references=references or None,
        )

        template_name = TEMPLATE_MAP.get(
            st.session_state.get("template_label"),
            "Blue Theme.html",
        )

        pdf_bytes = render_cv_pdf_bytes(cv, template_name=template_name)
        docx_bytes = render_cv_docx_bytes(cv)

        st.success("CV generated successfully! 🎉")

        col_cv1, col_cv2 = st.columns(2)
        with col_cv1:
            st.download_button(
                "📄 Download CV as PDF",
                data=pdf_bytes,
                file_name="cv.pdf",
                mime="application/pdf",
            )

        with col_cv2:
            st.download_button(
                "📝 Download CV as Word (.docx)",
                data=docx_bytes,
                file_name="cv.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

        # Optional analytics only
        st.session_state["cv_generations"] = st.session_state.get("cv_generations", 0) + 1
        increment_usage(email_for_usage, "cv_generations")

    except Exception as e:
        st.error(f"CV generation failed: {e}")
        st.stop()









# -------------------------
# Pricing (SUBSCRIPTIONS)
# -------------------------
st.header("Pricing")

col_free, col_monthly, col_pro = st.columns(3)

email_for_checkout = (st.session_state.get("user") or {}).get("email")

with col_free:
    st.subheader("Free")
    st.markdown(
        "**£0 / month**\n\n"
        "- Sign in required for downloads + AI tools\n"
        "- Includes a small starter allowance (if enabled): **5 CV + 5 AI**\n"
        "- CV templates included\n"
        "- Upgrade anytime\n"
    )

with col_monthly:
    st.subheader("Monthly")
    st.markdown(
        "**£2.99 / month**\n\n"
        "- Monthly allowance: **20 CV + 30 AI**\n"
        "- PDF + Word downloads\n"
        "- Email support\n"
        "- Cancel anytime\n"
        "\n"
        "*(Unused credits may carry over if you’ve enabled stacking — otherwise they reset monthly.)*"
    )

    if st.button("Start Monthly Subscription", key="start_monthly_sub"):
        if not email_for_checkout:
            st.warning("Please sign in first.")
            st.stop()
        if not PRICE_MONTHLY:
            st.error("Missing STRIPE_PRICE_MONTHLY in Railway Variables.")
            st.stop()
        if not stripe.api_key:
            st.error("Missing STRIPE_SECRET_KEY in Railway Variables.")
            st.stop()

        try:
            url = create_subscription_checkout_session(
                PRICE_MONTHLY,
                pack="monthly",
                customer_email=email_for_checkout,
            )
            st.link_button("Continue to secure checkout", url)
        except Exception as e:
            st.error(f"Stripe error: {e}")

with col_pro:
    st.subheader("Pro")
    st.markdown(
        "**£5.99 / month**\n\n"
        "- Monthly allowance: **50 CV + 90 AI**\n"
        "- PDF + Word downloads\n"
        "- Priority support\n"
        "- Cancel anytime\n"
        "\n"
        "*(Unused credits may carry over if you’ve enabled stacking — otherwise they reset monthly.)*"
    )

    if st.button("Start Pro Subscription", key="start_pro_sub"):
        if not email_for_checkout:
            st.warning("Please sign in first.")
            st.stop()
        if not PRICE_PRO:
            st.error("Missing STRIPE_PRICE_PRO in Railway Variables.")
            st.stop()
        if not stripe.api_key:
            st.error("Missing STRIPE_SECRET_KEY in Railway Variables.")
            st.stop()

        try:
            url = create_subscription_checkout_session(
                PRICE_PRO,
                pack="pro",
                customer_email=email_for_checkout,
            )
            st.link_button("Continue to secure checkout", url)
        except Exception as e:
            st.error(f"Stripe error: {e}")

st.markdown("---")
st.subheader("Enterprise (organisations & programmes)")
st.markdown(
    "- For organisations running employability or workforce programmes\n"
    "- Provide access for participants without individual charges\n"
    "- Suitable for charities, training providers, community organisations and public-sector programmes\n"
    "- Option to pilot locally in Walsall, then scale regionally/nationally\n"
    "- Includes onboarding and support\n"
    "\n"
    "**Enquire:** support@affiliateworldcommissions.com\n"
)

st.caption(
    "Subscriptions fund the platform and prevent abuse. "
    "If you're running a programme (council/charity/organisation), ask about Enterprise licensing."
)




# ==============================================
# FOOTER POLICY BUTTONS (snapshot before navigate)
# ============================================================
st.markdown("<hr style='margin-top:40px;'>", unsafe_allow_html=True)

fc1, fc2, fc3, fc4 = st.columns(4)
with fc1:
    if st.button("Accessibility", key="footer_accessibility"):
        snapshot_form_state()
        st.session_state["policy_view"] = "accessibility"
        st.rerun()
with fc2:
    if st.button("Cookie Policy", key="footer_cookies"):
        snapshot_form_state()
        st.session_state["policy_view"] = "cookies"
        st.rerun()
with fc3:
    if st.button("Privacy Policy", key="footer_privacy"):
        snapshot_form_state()
        st.session_state["policy_view"] = "privacy"
        st.rerun()
with fc4:
    if st.button("Terms of Use", key="footer_terms"):
        snapshot_form_state()
        st.session_state["policy_view"] = "terms"
        st.rerun()