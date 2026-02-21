# ============================================================
# APP START (CLEAN) — DROP IN FROM TOP OF FILE TO END OF CSS# ============================================================

import os
import io
import csv
import re
import time
import hashlib
import traceback
from datetime import datetime, timezone

import streamlit as st
import requests
import stripe
import psycopg2
import psycopg2.extras
from psycopg2.extras import RealDictCursor

from openai import OpenAI

from db import get_conn, get_db_connection, fetchone, fetchall, execute

from utils import (
    verify_postgres_connection,
    render_cv_pdf_bytes,
    render_cover_letter_pdf_bytes,
    render_cv_docx_bytes,
    render_cover_letter_docx_bytes,
)
from models import CV, Experience, Education
from ai_v2 import (
    generate_tailored_summary,
    generate_cover_letter_ai,
    improve_bullets,
    improve_skills,
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

# -------------------------
# CSS
# -------------------------
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
    unsafe_allow_html=True,
)


# ============================================================
# CONSTANTS (ONE PLACE ONLY)
# ============================================================
MAX_PANEL_WORDS = 100
MAX_DOC_WORDS = 300
MAX_LETTER_WORDS = 300
COOLDOWN_SECONDS = 5

# ============================================================
# GLOBAL PLAN + REFERRAL CONFIG
# ============================================================
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

# -------------------------
# STRIPE / URLS
# -------------------------
stripe.api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
PRICE_MONTHLY = (os.getenv("STRIPE_PRICE_MONTHLY") or "").strip()
PRICE_PRO = (os.getenv("STRIPE_PRICE_PRO") or "").strip()

APP_URL = (os.getenv("APP_URL") or "").strip() or "http://localhost:8501"

# ============================================================
# SESSION STATE DEFAULTS (EARLY, ONE PLACE)
# ============================================================
DEFAULT_SESSION_KEYS = {
    "user": None,
    "user_id": None,

    "accepted_policies": False,
    "chk_policy_agree": False,

    # policy modal state
    "footer_policy_open": False,
    "footer_policy_slug": None,
    "gate_policy_open": False,
    "gate_policy_slug": None,

    # policy content cache
    "_policies_loaded": False,
    "_policies": {},

    # job search cache
    "adzuna_results": [],

    # CV upload cache (stable across reruns)
    "cv_upload_bytes": None,
    "cv_upload_name": None,
}

for k, v in DEFAULT_SESSION_KEYS.items():
    st.session_state.setdefault(k, v)

# DB init stays here (once-per-session; Streamlit reruns on every interaction)
if not st.session_state.get("_db_ready"):
    init_db()
    verify_postgres_connection()
    st.session_state["_db_ready"] = True

PROTECTED_EXACT_KEYS = {
    # auth/user
    "user", "user_id", "accepted_policies", "chk_policy_agree",

    # caches/results
    "adzuna_results", "selected_job",

    # AI outputs to persist (non-widget)
    "job_summary_ai", "cover_letter", "cover_letter_box",

    # structure / derived state (non-widget)
    "education_items", "parsed_num_experiences",

    # cached upload payload (non-widget)
    "cv_upload_bytes", "cv_upload_name",
}

# Widget keys must never be restored from snapshots.
WIDGET_EXACT_KEYS = {
    "cv_uploader",
    "skills_text", "references", "job_description", "template_label",
    "num_experiences", "num_education",
    "adzuna_keywords", "adzuna_location",
}

WIDGET_PREFIXES = (
    "cv_",
    "job_title_", "company_", "exp_location_", "start_date_", "end_date_", "description_",
    "degree_", "institution_", "edu_location_", "edu_start_", "edu_end_",
)

SYSTEM_PREFIXES = (
    "_cooldown_",
    "__pending__",   # staged values
)

def is_widget_key(k: str) -> bool:
    if k in WIDGET_EXACT_KEYS:
        return True
    return any(k.startswith(p) for p in WIDGET_PREFIXES)

def is_protected_key(k: str) -> bool:
    if is_widget_key(k):
        return True
    return k.startswith((
        "cv_",
        "skills_",
        "job_title_",
        "company_",
        "description_",
        "degree_",
        "institution_",
        "edu_",
        "references",
        "template_label",
        "job_description",
    ))

def snapshot_protected_state(label=None):
    snap = {}
    for k, v in st.session_state.items():
        if is_widget_key(k):
            continue
        if is_protected_key(k):
            snap[k] = v

    st.session_state["_protected_snapshot"] = snap
    if label:
        st.session_state["_protected_snapshot_label"] = label
    return snap

def restore_protected_state(snap: dict) -> None:
    if not isinstance(snap, dict):
        return
    for k, v in snap.items():
        if is_widget_key(k):
            continue
        st.session_state[k] = v

def _safe_set(k, v):
    if v is None:
        return
    if isinstance(v, str) and not v.strip():
        return
    cur = st.session_state.get(k)
    if cur is None or (isinstance(cur, str) and not cur.strip()):
        st.session_state[k] = v.strip() if isinstance(v, str) else v

# ---------- Safe session ops ----------
def safe_pop_state(k: str) -> None:
    if is_protected_key(k):
        return
    st.session_state.pop(k, None)

def safe_clear_state(keys: list[str]) -> None:
    state_debug_capture("safe_clear_state:before")
    for k in keys:
        if is_widget_key(k):
            continue
        safe_pop_state(k)
    state_debug_capture("safe_clear_state:after")

def safe_init_key(key: str, default=""):
    if key not in st.session_state or st.session_state[key] is None:
        st.session_state[key] = default

def stage_value(key: str, value):
    st.session_state[f"__pending__{key}"] = value

def apply_staged_value(key: str):
    pk = f"__pending__{key}"
    if pk in st.session_state:
        st.session_state[key] = st.session_state.pop(pk)

def safe_set_if_missing(key: str, value, *, strip: bool = True):
    cur = st.session_state.get(key)
    cur_s = (cur or "").strip() if isinstance(cur, str) else cur
    if cur is None or cur_s == "":
        if isinstance(value, str) and strip:
            value = value.strip()
        if value is not None:
            st.session_state[key] = value


# ---------- State forensic debugger ----------
STATE_DEBUG_STATIC_KEYS = {
    "cv_full_name", "cv_email", "cv_phone", "cv_location", "cv_title", "cv_summary",
    "skills_text", "references", "job_description", "template_label",
    "num_experiences", "num_education",
    "_just_autofilled_from_cv", "_last_cv_fingerprint", "_pending_cv_parsed",
}
STATE_DEBUG_PREFIXES = (
    "job_title_", "company_", "description_", "start_date_", "end_date_",
    "degree_", "institution_", "edu_start_", "edu_end_", "cv_",
)


def _state_debug_should_track(key: str) -> bool:
    return key in STATE_DEBUG_STATIC_KEYS or key.startswith(STATE_DEBUG_PREFIXES)


def _state_debug_is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _state_debug_fingerprint(value) -> str:
    kind = type(value).__name__
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return "str:<empty>"
        head = txt.replace("\n", " ")[:60]
        h = hashlib.sha1(txt.encode("utf-8", errors="ignore")).hexdigest()[:8]
        return f"str:len={len(txt)} hash={h} '{head}'"
    if isinstance(value, (list, tuple, set)):
        return f"{kind}:len={len(value)}"
    if isinstance(value, dict):
        return f"dict:keys={len(value)}"
    return f"{kind}:{repr(value)[:80]}"


def _state_debug_snapshot() -> dict:
    snap = {}
    for k, v in st.session_state.items():
        if _state_debug_should_track(k):
            snap[k] = {
                "empty": _state_debug_is_empty(v),
                "fp": _state_debug_fingerprint(v),
            }
    return snap


def state_debug_capture(tag: str) -> None:
    current = _state_debug_snapshot()
    prev = st.session_state.get("_state_debug_prev_snapshot") or {}

    removed = sorted([k for k in prev.keys() if k not in current])
    emptied = sorted([
        k for k in current.keys() & prev.keys()
        if prev[k].get("empty") is False and current[k].get("empty") is True
    ])
    overwritten = sorted([
        k for k in current.keys() & prev.keys()
        if prev[k].get("fp") != current[k].get("fp")
    ])

    events = st.session_state.get("_state_debug_events", [])
    events.append({
        "tag": tag,
        "removed": removed,
        "emptied": emptied,
        "overwritten": overwritten,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    st.session_state["_state_debug_events"] = events[-30:]
    st.session_state["_state_debug_prev_snapshot"] = current
    st.session_state["_state_debug_last_tag"] = tag


def state_debug_report(tag: str = "run") -> None:
    st.session_state["_state_debug_report_tag"] = tag
    with st.expander("State Debug", expanded=False):
        st.caption(f"Last capture tag: {st.session_state.get('_state_debug_last_tag', 'n/a')}")
        events = st.session_state.get("_state_debug_events", [])
        if not events:
            st.write("No state debug events yet.")
        else:
            for ev in events[-8:]:
                st.markdown(f"**{ev.get('ts', '')}** `{ev.get('tag', '')}`")
                st.write({
                    "keys_removed": ev.get("removed", []),
                    "non_empty_to_empty": ev.get("emptied", []),
                    "keys_overwritten": ev.get("overwritten", []),
                })

    # Wipe Tripwire: any cv_* key going non-empty -> empty in a single transition
    events = st.session_state.get("_state_debug_events", [])
    if events:
        last = events[-1]
        cv_emptied = [k for k in last.get("emptied", []) if k.startswith("cv_")]
        if cv_emptied:
            st.error(
                "Wipe Tripwire triggered: cv_* key(s) changed from non-empty to empty in one rerun. "
                f"Keys: {cv_emptied}. Last tag: {last.get('tag', 'unknown')}"
            )
            st.stop()



def _apply_parsed_fallback(parsed: dict) -> None:
    """
    Fallback mapping if _apply_parsed_cv_to_session isn't available.
    Only sets missing fields (never overwrites user edits).
    """
    if not isinstance(parsed, dict):
        return

    # --- skills ---
    skills = parsed.get("skills")
    if isinstance(skills, list):
        joined = "\n".join(
            f"• {str(s).strip()}" for s in skills if str(s).strip()
        )
        if joined.strip():
            safe_set_if_missing("skills_text", joined)
    elif isinstance(skills, str) and skills.strip():
        safe_set_if_missing("skills_text", skills.strip())

    # --- experiences ---
    exps = parsed.get("experiences") or parsed.get("experience") or []
    if isinstance(exps, list) and exps:
        n = max(1, min(5, len(exps)))
        st.session_state["parsed_num_experiences"] = n
        safe_set_if_missing("num_experiences", n)

        for i in range(n):
            e = exps[i] or {}
            safe_set_if_missing(f"job_title_{i}", e.get("job_title") or e.get("title") or "")
            safe_set_if_missing(f"company_{i}", e.get("company") or e.get("employer") or "")
            safe_set_if_missing(f"exp_location_{i}", e.get("location") or "")
            safe_set_if_missing(f"start_date_{i}", e.get("start_date") or e.get("start") or "")
            safe_set_if_missing(f"end_date_{i}", e.get("end_date") or e.get("end") or "")
            desc = e.get("description") or ""
            if isinstance(desc, list):
                desc = "\n".join(str(x).strip() for x in desc if str(x).strip())
            safe_set_if_missing(f"description_{i}", desc or "")

    # --- education ---
    edu = parsed.get("education") or parsed.get("educations") or []
    if isinstance(edu, list) and edu:
        n = max(1, min(5, len(edu)))
        safe_set_if_missing("num_education", n)

        for i in range(n):
            r = edu[i] or {}
            safe_set_if_missing(f"degree_{i}", r.get("degree") or r.get("qualification") or "")
            safe_set_if_missing(f"institution_{i}", r.get("institution") or r.get("school") or "")
            safe_set_if_missing(f"edu_location_{i}", r.get("location") or r.get("city") or "")
            safe_set_if_missing(f"edu_start_{i}", r.get("start_date") or r.get("start") or "")
            safe_set_if_missing(f"edu_end_{i}", r.get("end_date") or r.get("end") or "")

    # --- skills ---
    skills = parsed.get("skills")
    if isinstance(skills, list):
        joined = "\n".join(
            f"• {str(s).strip()}" for s in skills if str(s).strip()
        )
        if joined.strip():
            safe_set_if_missing("skills_text", joined)
    elif isinstance(skills, str) and skills.strip():
        safe_set_if_missing("skills_text", skills.strip())

    # --- experiences ---
    exps = parsed.get("experiences") or parsed.get("experience") or []
    if isinstance(exps, list) and exps:
        n = max(1, min(5, len(exps)))
        st.session_state["parsed_num_experiences"] = n
        safe_set_if_missing("num_experiences", n)

        for i in range(n):
            e = exps[i] or {}
            safe_set_if_missing(f"job_title_{i}", e.get("job_title") or e.get("title") or "")
            safe_set_if_missing(f"company_{i}", e.get("company") or e.get("employer") or "")
            safe_set_if_missing(f"exp_location_{i}", e.get("location") or "")
            safe_set_if_missing(f"start_date_{i}", e.get("start_date") or e.get("start") or "")
            safe_set_if_missing(f"end_date_{i}", e.get("end_date") or e.get("end") or "")
            desc = e.get("description") or ""
            if isinstance(desc, list):
                desc = "\n".join(str(x).strip() for x in desc if str(x).strip())
            safe_set_if_missing(f"description_{i}", desc or "")

    # --- education ---
    edu = parsed.get("education") or parsed.get("educations") or []
    if isinstance(edu, list) and edu:
        n = max(1, min(5, len(edu)))
        safe_set_if_missing("num_education", n)

        for i in range(n):
            r = edu[i] or {}
            safe_set_if_missing(f"degree_{i}", r.get("degree") or r.get("qualification") or "")
            safe_set_if_missing(f"institution_{i}", r.get("institution") or r.get("school") or "")
            safe_set_if_missing(f"edu_location_{i}", r.get("location") or r.get("city") or "")
            safe_set_if_missing(f"edu_start_{i}", r.get("start_date") or r.get("start") or "")
            safe_set_if_missing(f"edu_end_{i}", r.get("end_date") or r.get("end") or "")

# ---------- Word limit helper (you call this in multiple places) ----------
def enforce_word_limit(text: str, max_words: int, label: str = "") -> str:
    words = (text or "").split()
    if len(words) > int(max_words):
        st.warning(
            f"{label or 'Text'} is limited to {max_words} words. "
            f"Currently {len(words)}; extra words are ignored."
        )
        return " ".join(words[: int(max_words)])
    return text or ""

# ---------- User ID helpers (NO recursion, no globals tricks) ----------
def get_user_id_by_email(email: str) -> int | None:
    email = (email or "").strip().lower()
    if not email:
        return None
    row = fetchone(
        "SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1",
        (email,),
    )
    if not row:
        return None
    try:
        return int(row["id"])
    except Exception:
        return None

def get_user_id(email: str) -> int | None:
    return get_user_id_by_email(email)

# ============================================================
# LEDGER CREDITS — SINGLE SOURCE OF TRUTH (NO NAME CLASH)
# - supports BOTH styles:
#     spend_credits(conn, uid, source=..., cv_amount=1)
#     spend_credits(uid, source=..., cv=1)
# - fixes: unexpected keyword 'cv_amount'
# - prevents recursion bugs
# ============================================================

def get_credits_by_user_id(user_id: int) -> dict:
    user_id = int(user_id)
    row = fetchone(
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
    ) or {}
    return {"cv": int(row.get("cv", 0) or 0), "ai": int(row.get("ai", 0) or 0)}


def _get_credits_by_user_id_on_conn(conn, user_id: int) -> dict:
    user_id = int(user_id)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
        return {"cv": int(row.get("cv", 0) or 0), "ai": int(row.get("ai", 0) or 0)}


def spend_credits_on_conn(conn, user_id: int, *, source: str, cv_amount: int = 0, ai_amount: int = 0) -> bool:
    """
    Atomic spend USING THE SAME CONNECTION.
    """
    user_id = int(user_id)
    cv_amount = int(cv_amount or 0)
    ai_amount = int(ai_amount or 0)
    source = (source or "").strip()
    if not source:
        return False

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user_id,))
            if not cur.fetchone():
                conn.rollback()
                return False

            bal = _get_credits_by_user_id_on_conn(conn, user_id)

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


def spend_credits(*args, **kwargs) -> bool:
    """
    Backwards-compatible dispatcher.

    Accepts:
      1) spend_credits(conn, uid, source="x", cv_amount=1, ai_amount=0)
      2) spend_credits(uid, source="x", cv=1, ai=0)

    Also accepts cv_amount/ai_amount even in style #2, so old calls won't crash.
    """
    # ---- detect if first arg is a DB connection ----
    if len(args) >= 2 and hasattr(args[0], "cursor"):
        conn = args[0]
        user_id = int(args[1])
        source = (kwargs.get("source") or "").strip()

        cv_amount = kwargs.get("cv_amount", kwargs.get("cv", 0))
        ai_amount = kwargs.get("ai_amount", kwargs.get("ai", 0))

        return spend_credits_on_conn(
            conn, user_id, source=source,
            cv_amount=int(cv_amount or 0), ai_amount=int(ai_amount or 0)
        )

    # ---- style #2 (no conn provided) ----
    if len(args) < 1:
        return False

    user_id = int(args[0])
    source = (kwargs.get("source") or "").strip()

    cv_amount = kwargs.get("cv_amount", kwargs.get("cv", 0))
    ai_amount = kwargs.get("ai_amount", kwargs.get("ai", 0))

    if not source:
        return False

    with get_conn() as conn:
        return spend_credits_on_conn(
            conn, user_id, source=source,
            cv_amount=int(cv_amount or 0), ai_amount=int(ai_amount or 0)
        )


def try_spend(user_id: int, *, source: str, cv: int = 0, ai: int = 0) -> bool:
    """
    UI-friendly helper.
    """
    user_id = int(user_id)
    source = (source or "").strip()
    if not source:
        return False
    return bool(spend_credits(user_id, source=source, cv=int(cv or 0), ai=int(ai or 0)))


def spend_ai_credit(email: str, *, source: str, amount: int = 1) -> bool:
    """
    Email → uid → spend AI credits (1 by default)
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    uid = get_user_id_by_email(email)
    if not uid:
        return False
    return try_spend(int(uid), source=source, ai=int(amount or 1))

# ---------- locked_action_button (compatible with BOTH of your call styles) ----------
def locked_action_button(
    label: str,
    *,
    key: str,
    feature_label: str | None = None,   # your newer usage
    action_label: str | None = None,    # your older usage
    counter_key: str | None = None,
    require_login: bool = True,
    default_tab: str = "Sign in",
    cooldown_name: str | None = None,
    cooldown_seconds: int = 5,
    disabled: bool = False,
    **_ignored,  # absorbs unexpected kwargs safely
) -> bool:
    """
    Gate + cooldown only.
    IMPORTANT: does NOT clear state.
    """
    clicked = st.button(label, key=key, disabled=disabled)
    if not clicked:
        return False

    # login gate
    if require_login:
        u = st.session_state.get("user") or {}
        if not (isinstance(u, dict) and (u.get("email") or "").strip()):
            st.toast(f"🔒 Sign in to {feature_label or action_label or 'use this feature'}", icon="🔒")
            if "open_auth_modal" in globals():
                open_auth_modal(default_tab)
            st.stop()

    # cooldown gate
    if cooldown_name:
        ok, left = cooldown_ok(cooldown_name, cooldown_seconds)
        if not ok:
            st.warning(f"⏳ Please wait {left}s before trying again.")
            st.stop()

    return True

def restore_protected_state_if_needed():
    """
    Safety no-op restore helper.
    Keeps backward compatibility with older calls.
    Does NOT clear or modify session state.
    """
    return

def render_public_home():
    """
    Safe placeholder for guest / public landing.
    Keeps app running if the real implementation was removed.
    """
    return



# =========================
# OUTPUT RESET (NO RECURSION)
# Replace BOTH of your functions with these versions.
# =========================

def reset_outputs_only() -> None:
    state_debug_capture("reset_outputs_only:before")
    """
    Clears only generated/derived outputs.
    Does NOT touch user inputs (cv_*, skills_text, education fields, etc).
    MUST NOT call clear_ai_upload_state_only() (prevents recursion).
    """
    keys_to_clear = [
        "final_pdf_bytes",
        "final_docx_bytes",
        "download_ready",
        "generated_cv",
        "generated_cover_letter",
        "generated_summary",
        "suggested_bullets",
        "ats_score",
        "job_summary_ai",
        "selected_template",
    ]

    snap = snapshot_protected_state("reset_outputs_only")
    for k in keys_to_clear:
        safe_pop_state(k)
    restore_protected_state(snap)
    state_debug_capture("reset_outputs_only:after")


def clear_ai_upload_state_only() -> None:
    state_debug_capture("clear_ai_upload_state_only:before")
    """
    Clears only upload/parse transient flags and any derived outputs.
    (prevents recursion).
    """
    snap = snapshot_protected_state("clear_ai_upload_state_only")

    keys_to_clear = [
        "_cv_parsed",
        "_cv_autofill_enabled",
        "_just_autofilled_from_cv",
        "_last_cv_fingerprint",
    ]

    for k in keys_to_clear:
        safe_pop_state(k)

    for k in [
        "final_pdf_bytes",
        "final_docx_bytes",
        "download_ready",
        "generated_cv",
        "generated_cover_letter",
        "generated_summary",
        "suggested_bullets",
        "ats_score",
        "selected_template",
    ]:
        safe_pop_state(k)

    restore_protected_state(snap)
    state_debug_capture("clear_ai_upload_state_only:after")

# ============================================================
# POLICIES (MODAL ONLY — NO PAGE ROUTING)
# ============================================================
def ensure_policies_loaded() -> None:
    if st.session_state.get("_policies_loaded"):
        return

    base = os.path.join(os.path.dirname(__file__), "policies")
    mapping = {
        "accessibility": ("Accessibility", "accessibility.md"),
        "cookies": ("Cookie Policy", "cookie_policy.md"),
        "privacy": ("Privacy Policy", "privacy_policy.md"),
        "terms": ("Terms of Use", "terms_of_use.md"),
    }

    policies = {}
    for slug, (title, filename) in mapping.items():
        path = os.path.join(base, filename)
        body = ""
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    body = f.read()
        except Exception:
            body = ""
        policies[slug] = {"title": title, "body": body}

    st.session_state["_policies_loaded"] = True
    st.session_state["_policies"] = policies

def open_policy(scope: str, slug: str) -> None:
    st.session_state[f"{scope}_policy_open"] = True
    st.session_state[f"{scope}_policy_slug"] = slug

def close_policy(scope: str) -> None:
    st.session_state[f"{scope}_policy_open"] = False
    st.session_state[f"{scope}_policy_slug"] = None

def _policy_rerun():
    try:
        st.rerun()
    except Exception:
        st.experimental_rerun()

def render_policy_modal(scope: str) -> None:
    open_key = f"{scope}_policy_open"
    slug_key = f"{scope}_policy_slug"

    st.session_state.setdefault(open_key, False)
    st.session_state.setdefault(slug_key, None)

    if not st.session_state.get(open_key):
        return

    ensure_policies_loaded()

    slug = st.session_state.get(slug_key) or "privacy"
    pol = (st.session_state.get("_policies") or {}).get(slug) or {}
    title = pol.get("title") or "Policy"
    body = pol.get("body") or ""

    @st.dialog(title)
    def _dlg():
        if body.strip():
            st.markdown(body)
        else:
            st.info("Policy content not found in this deployment.")

        b1, b2 = st.columns(2)
        with b1:
            if st.button("← Back", key=f"{scope}_policy_back"):
                close_policy(scope)
                _policy_rerun()
        with b2:
            if st.button("Close", key=f"{scope}_policy_close"):
                close_policy(scope)
                _policy_rerun()

# ============================================================
# CV FILE READER (ONE COPY ONLY)
# ============================================================
def _read_uploaded_cv_bytes_to_text(name: str, data: bytes) -> str:
    """Read PDF/DOCX/TXT bytes into text."""
    if not data:
        return ""

    name = (name or "").lower()
    ext = os.path.splitext(name)[1]

    if ext == ".txt":
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1", errors="ignore")

    if ext == ".docx":
        import docx
        doc = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text)

    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            txt = page.extract_text() or ""
            if txt.strip():
                parts.append(txt)
        return "\n\n".join(parts)

    return ""

# ============================================================
# COOLDOWN (ONE COPY ONLY)
# ============================================================
def cooldown_ok(action_key: str, seconds: int = COOLDOWN_SECONDS):
    now = time.monotonic()
    last = st.session_state.get(f"_cooldown_{action_key}", 0.0)
    remaining = seconds - (now - last)
    if remaining > 0:
        return False, int(remaining) + 1
    st.session_state[f"_cooldown_{action_key}"] = now
    return True, 0


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

# run early every script start
restore_protected_state_if_needed()


# =========================
# EMAIL VALIDATION
# =========================
EMAIL_RE = re.compile(
    r"^(?=.{3,254}$)[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()

def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(normalize_email(email)))


# =========================
# AUTH MODAL STATE (DEFINE ONCE)
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

def set_logged_in_user(user: dict) -> None:
    """
    Sets session user ONLY. Does not touch CV state.
    """
    if not (isinstance(user, dict) and user.get("email")):
        return

    st.session_state["user"] = user

    # keep these consistent
    st.session_state["accepted_policies"] = bool(user.get("accepted_policies"))
    st.session_state["chk_policy_agree"] = False

    # close auth modal
    st.session_state["auth_modal_open"] = False
    st.rerun()

def do_logout() -> None:
    """
    Logout should NOT wipe CV draft state (your requirement).
    It should only remove identity + gating flags.
    """
    st.session_state["user"] = None
    st.session_state["user_id"] = None

    st.session_state["accepted_policies"] = False
    st.session_state["chk_policy_agree"] = False

    st.session_state["auth_modal_open"] = False
    st.session_state["auth_modal_tab"] = "Sign in"
    st.session_state["auth_modal_epoch"] = st.session_state.get("auth_modal_epoch", 0) + 1

    st.rerun()


# =========================
# CONSENT GATE (POST-LOGIN ONLY) - FAIL CLOSED
# Policy readouts are MODAL ONLY (no accept inside policy modal)
# =========================
def show_consent_gate() -> None:
    user = st.session_state.get("user")
    if not (isinstance(user, dict) and user.get("email")):
        return

    email = normalize_email(user.get("email"))
    if not email:
        return

    try:
        accepted_in_db = bool(has_accepted_policies(email))
    except Exception as e:
        st.error(f"Policy check failed. Please refresh and try again. ({repr(e)})")
        st.stop()

    st.session_state["accepted_policies"] = accepted_in_db
    if accepted_in_db:
        return

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
        if st.button("Cookie Policy", key="gate_open_cookies"):
            open_policy("gate", "cookies")
    with c2:
        if st.button("Privacy Policy", key="gate_open_privacy"):
            open_policy("gate", "privacy")
    with c3:
        if st.button("Terms of Use", key="gate_open_terms"):
            open_policy("gate", "terms")

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
            st.error(f"Could not save your acceptance. Please try again. ({repr(e)})")
            st.stop()

        # update session
        u = st.session_state.get("user") or {}
        if isinstance(u, dict):
            u["accepted_policies"] = True
            st.session_state["user"] = u

        st.session_state["accepted_policies"] = True
        st.session_state["chk_policy_agree"] = False
        st.rerun()

    render_policy_modal("gate")
    st.info("Please accept to continue using the site.")
    st.stop()


# =========================
# AUTH UI
# =========================
def auth_ui():
    tab_login, tab_register, tab_forgot = st.tabs(
        ["Sign in", "Create account", "Forgot password"]
    )

    # ---- LOGIN TAB ----
    with tab_login:
        login_email = st.text_input("Email", key="auth_login_email")
        login_password = st.text_input("Password", type="password", key="auth_login_password")

        if st.button("Sign in", key="auth_btn_login"):
            if not login_email or not login_password:
                st.error("Please enter both email and password.")
                st.stop()

            login_email_n = normalize_email(login_email)
            if not is_valid_email(login_email_n):
                st.error("Please enter a valid email address.")
                st.stop()

            user = authenticate_user(login_email_n, login_password)
            if user:
                st.success(f"Welcome back, {user.get('full_name') or user['email']}!")
                set_logged_in_user(user)
            else:
                st.error("Invalid email or password.")

    # ---- REGISTER TAB ----
    with tab_register:
        reg_name = st.text_input("Full name", key="auth_reg_name")
        reg_email = st.text_input("Email", key="auth_reg_email")
        reg_password = st.text_input("Password", type="password", key="auth_reg_password")
        reg_password2 = st.text_input("Confirm password", type="password", key="auth_reg_password2")

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

            reg_email_n = normalize_email(reg_email)
            if not is_valid_email(reg_email_n):
                st.error("Please enter a valid email address (e.g. name@example.com).")
                st.stop()

            referral_code = None
            if reg_referral_code.strip():
                ref_user = get_user_by_referral_code(reg_referral_code.strip())
                if not ref_user:
                    st.error("That referral code is not valid.")
                    st.stop()
                referral_code = reg_referral_code.strip().upper()

            ok = create_user(
                email=reg_email_n,
                password=reg_password,
                full_name=reg_name,
                referred_by=referral_code,
            )
            if not ok:
                st.error("That email is already registered.")
                st.stop()

            new_user = get_user_by_email(reg_email_n)
            if new_user and new_user.get("id") is not None:
                # must exist in your codebase
                grant_starter_credits(int(new_user["id"]))

            if referral_code:
                try:
                    apply_referral_bonus(
                        new_user_email=reg_email_n,
                        referral_code=referral_code,
                    )
                except Exception:
                    pass

            user = authenticate_user(reg_email_n, reg_password)
            if user:
                st.session_state["accepted_policies"] = False
                st.session_state["chk_policy_agree"] = False
                st.success("Account created. Please accept policies to continue.")
                set_logged_in_user(user)
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
                    token = create_password_reset_token(fp_email)
                    if token:
                        send_password_reset_email(fp_email, token)
                    st.success("If this email is registered, a reset link has been sent.")
                except Exception as e:
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
                    st.success("Password reset successfully. You can now sign in.")
                else:
                    st.error("Invalid or expired reset token. Please request a new reset link.")


# =========================
# AUTH DIALOG (DEFINED ONCE)
# =========================
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

    auth_ui()

    if st.button("Close", key=f"auth_modal_close_{st.session_state.get('auth_modal_epoch', 0)}"):
        close_auth_modal()

def render_auth_modal_if_open() -> None:
    if st.session_state.get("auth_modal_open", False):
        _auth_dialog()


state_debug_capture("run:start")

# =========================
# ROUTING (EARLY) — ONE VERSION ONLY
# =========================
def get_user_context():
    u = st.session_state.get("user") or {}
    logged_in = _is_logged_in_user(u)
    email = normalize_email(u.get("email")) if logged_in else None
    admin = bool(logged_in and u.get("role") in {"owner", "admin"})
    role = (u.get("role") or "user") if logged_in else "guest"
    return u, logged_in, email, admin, role

current_user, is_logged_in, user_email, is_admin, user_role = get_user_context()

# Non-blocking overlays / dialogs
render_auth_modal_if_open()

# Guest home header (MUST exist ABOVE this call in your file)
if not is_logged_in:
    # make sure render_public_home() is defined before this chunk runs
    render_public_home()

# Consent gate (only triggers when logged in)
show_consent_gate()

# Cache uid for DB ops (logged in only)
if user_email:
    uid = get_user_id_by_email(user_email)
    if not uid:
        st.error("No account found for this email. Please sign out and sign in again.")
        st.stop()
    st.session_state["user_id"] = uid


# =========================
# ADMIN DASHBOARD (unchanged logic)
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
                do_logout()
            st.rerun()


# =========================
# MODE SELECT (ADMIN ONLY)
# =========================
if is_admin:
    mode = st.sidebar.radio("Mode", ["Use app", "Admin dashboard"], index=0, key="mode_select")
else:
    mode = "Use app"

if mode == "Admin dashboard":
    render_admin_dashboard()
    st.stop()


# =========================
# SIDEBAR (KEEP DRAFT STATE)
# =========================
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
        with c2:
            if st.button("✨ Create", key="brand_create_btn"):
                open_auth_modal("Create account")

with st.sidebar:
    # always re-pull
    session_user, sidebar_logged_in, email0, sidebar_is_admin, sidebar_role = get_user_context()

    # Brand
    render_mulyba_brand_header(sidebar_logged_in)

    # Mode badge
    if sidebar_logged_in:
        st.markdown(
            """<div class="mode-badge mode-live"><span class="dot"></span> Live mode</div>""",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """<div class="mode-badge mode-guest"><span class="dot"></span> Guest mode</div>""",
            unsafe_allow_html=True,
        )

    # Account
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
        # OPTIONAL: if you have refresh_session_user_from_db() keep it; else remove this try.
        try:
            refresh_session_user_from_db()
        except Exception:
            pass

        session_user = st.session_state.get("user") or {}
        full_name = session_user.get("full_name") or "Member"
        email = session_user.get("email") or "—"
        plan = (session_user.get("plan") or "free").strip().lower()
        plan_label = "Pro" if plan == "pro" else ("Monthly" if plan == "monthly" else "Free")

        st.markdown(f"**{full_name}**")
        st.markdown(f'<div class="sb-muted">{email}</div>', unsafe_allow_html=True)
        st.markdown(f"**Plan:** {plan_label}")

        if sidebar_role in {"owner", "admin"}:
            st.caption(f"Admin: {sidebar_role}")

        is_banned = bool(session_user.get("is_banned"))
        st.markdown(f"**Status:** {'🚫 Banned' if is_banned else '✅ Active'}")

        accepted = bool(session_user.get("accepted_policies"))
        st.markdown(f"**Policies accepted:** {'Yes' if accepted else 'No'}")

        if st.button("Log out", key="sb_logout_btn"):
            do_logout()

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
        # ✅ session_user already refreshed above, but keep safe if this block is used elsewhere
        session_user = st.session_state.get("user") or {}

        # Admin unlimited
        if sidebar_role in {"owner", "admin"}:
            st.markdown("**CV Generations:** ♾️ Unlimited")
            st.markdown("**AI Tools:** ♾️ Unlimited")
        else:
            email = ((session_user or {}).get("email") or "").strip().lower()

            # ✅ Robust UID: use session id if present, else compute from email
            uid = (session_user or {}).get("id")
            if not uid and email:
                uid = get_user_id(email)

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

            st.markdown(f"**CV Remaining:** {cv_left}")
            st.progress(cv_left / cv_total_session)

            st.markdown(f"**AI Remaining:** {ai_left}")
            st.progress(ai_left / ai_total_session)

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
"""
    )

    st.markdown("</div>", unsafe_allow_html=True)

def apply_pending_autofill_if_any():
    parsed = st.session_state.pop("_pending_cv_parsed", None)
    if not isinstance(parsed, dict):
        return

    if "_apply_parsed_cv_to_session" in globals() and callable(globals()["_apply_parsed_cv_to_session"]):
        globals()["_apply_parsed_cv_to_session"](parsed)
    else:
        _apply_parsed_fallback(parsed)

    safe_set_if_missing("cv_full_name", parsed.get("full_name") or parsed.get("name") or "")
    safe_set_if_missing("cv_email", parsed.get("email") or "")
    safe_set_if_missing("cv_phone", parsed.get("phone") or "")
    safe_set_if_missing("cv_location", parsed.get("location") or "")
    safe_set_if_missing("cv_title", parsed.get("title") or parsed.get("professional_title") or parsed.get("current_title") or "")
    safe_set_if_missing("cv_summary", parsed.get("summary") or parsed.get("professional_summary") or "")

    st.session_state["_just_autofilled_from_cv"] = True


def section_cv_upload():
    st.subheader("Upload an existing CV (optional)")
    st.caption("Upload a PDF/DOCX/TXT, then let AI fill the form for you.")

    uploaded_cv = st.file_uploader(
        "Upload your current CV (PDF, DOCX or TXT)",
        type=["pdf", "docx", "txt"],
        key="cv_uploader",
    )

    # Persist bytes+name across reruns
    if uploaded_cv is not None:
        data = uploaded_cv.getvalue() if hasattr(uploaded_cv, "getvalue") else uploaded_cv.read()
        if data:
            st.session_state["cv_upload_bytes"] = data
            st.session_state["cv_upload_name"] = getattr(uploaded_cv, "name", "uploaded_cv")

    fill_clicked = locked_action_button(
        "Fill the form from this CV (AI)",
        key="btn_fill_from_cv",
        feature_label="CV upload & parsing",
        counter_key="upload_parses",
        require_login=True,
        default_tab="Sign in",
        cooldown_name="upload_parse",
        cooldown_seconds=5,
    )

    if fill_clicked:
        cv_upload_bytes = st.session_state.get("cv_upload_bytes")
        cv_upload_name = st.session_state.get("cv_upload_name")

        if not cv_upload_bytes:
            st.warning("Upload a CV first.")
            st.stop()

        raw_text = _read_uploaded_cv_bytes_to_text(cv_upload_name, cv_upload_bytes)
        if not (raw_text or "").strip():
            st.warning("Please upload a readable PDF, DOCX, or TXT CV first.")
            st.stop()

        with st.spinner("Reading and analysing your CV..."):
            parsed = extract_cv_data(raw_text)

        if not isinstance(parsed, dict):
            st.error("AI parser returned an unexpected format.")
            st.stop()

        # IMPORTANT: stage parsed for NEXT run (don’t write to widgets in same run)
        st.session_state["_pending_cv_parsed"] = parsed

        st.success("CV parsed. Applying to the form...")
        st.rerun()
		

apply_pending_autofill_if_any()
section_cv_upload()   # ✅ THIS LINE IS MISSING IN YOUR CODE

# -------------------------
# 1. Personal details
# -------------------------
st.header("1. Personal details")

for k in ["cv_full_name", "cv_title", "cv_email", "cv_phone", "cv_location", "cv_summary"]:
    safe_init_key(k, "")
    apply_staged_value(k)

cv_full_name = st.text_input("Full name *", key="cv_full_name")
cv_title     = st.text_input("Professional title (e.g. Software Engineer)", key="cv_title")
cv_email     = st.text_input("Email *", key="cv_email")
cv_phone     = st.text_input("Phone", key="cv_phone")
cv_location  = st.text_input("Location (City, Country)", key="cv_location")

cv_summary_text = st.text_area("Professional summary", height=120, key="cv_summary")

MAX_PANEL_WORDS = globals().get("MAX_PANEL_WORDS", 100)
st.caption(f"Tip: keep this under {MAX_PANEL_WORDS} words – extra text will be ignored.")

btn_summary = st.button("Improve professional summary (AI)", key="btn_improve_summary")

if btn_summary:
    if not gate_premium("improve your professional summary"):
        st.stop()

    ok, left = cooldown_ok("improve_summary", 5)
    if not ok:
        st.warning(f"⏳ Please wait {left}s before trying again.")
        st.stop()

    if not cv_summary_text.strip():
        st.error("Please write a professional summary first.")
        st.stop()

    email_for_usage = (st.session_state.get("user") or {}).get("email")
    if not email_for_usage:
        st.warning("Please sign in to use AI features.")
        st.stop()

    ok_spend = spend_ai_credit(email_for_usage, source=f"ai_summary_improve:{int(time.time())}", amount=1)
    if not ok_spend:
        st.warning("You don’t have enough AI credits for this action.")
        st.stop()

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
                "new experience, only polish what is already there."
            )

            improved = generate_tailored_summary(cv_like, instructions)
            improved = enforce_word_limit(improved, MAX_PANEL_WORDS, label="Professional summary")

            stage_value("cv_summary", improved)

            st.session_state["summary_uses"] = st.session_state.get("summary_uses", 0) + 1
            increment_usage(email_for_usage, "summary_uses")

            st.success("AI summary applied.")
            st.rerun()

        except Exception as e:
            st.error(f"AI error (summary improvement): {e}")
            st.stop()

        except Exception as e:
            restore_protected_state(snap)
            st.error(f"AI error (summary improvement): {e}")
            st.stop()


# -------------------------
# 2. Skills (bullet points only)
# -------------------------
def normalize_skills_to_bullets(text: str) -> str:
    if not text:
        return ""
    raw = text.strip()
    if not raw:
        return ""

    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    items: list[str] = []

    def is_sentence(s: str) -> bool:
        return len(s.split()) > 6 or "," in s or "result" in s.lower() or "through" in s.lower()

    for ln in lines:
        ln = ln.lstrip("•*-–— \t").strip()
        if not ln:
            continue

        parts = [p.strip() for p in ln.split(",") if p.strip()] if "," in ln else [ln]
        for p in parts:
            if is_sentence(p):
                words = p.split()
                if len(words) >= 2:
                    items.append(" ".join(words[:3]))
            else:
                items.append(p)

    seen = set()
    clean: list[str] = []
    for it in items:
        it = it.strip().title()
        if it and it.lower() not in seen:
            seen.add(it.lower())
            clean.append(it)

    return "\n".join(f"• {c}" for c in clean)

# apply staged before widget
safe_init_key("skills_text", "")
apply_staged_value("skills_text")

skills_text = st.text_area(
    "Skills (one per line)",
    key="skills_text",
    help="Use short skill phrases only (1–3 words per line)",
)

btn_skills = st.button("Improve skills (AI)", key="btn_improve_skills")

if btn_skills:
    state_debug_capture("btn_improve_skills:clicked")
    snap = snapshot_protected_state("before_ai_skills")

    if not gate_premium("improve your skills"):
        st.stop()

    ok, left = cooldown_ok("improve_skills", 5)
    if not ok:
        st.warning(f"⏳ Please wait {left}s before trying again.")
        st.stop()

    if not skills_text.strip():
        st.warning("Please add some skills first.")
        st.stop()

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
            improved = improve_skills(skills_text)
            improved_bullets = normalize_skills_to_bullets(improved)
            improved_limited = enforce_word_limit(improved_bullets, MAX_DOC_WORDS, label="Skills (AI)")

            stage_value("skills_text", improved_limited)
            restore_protected_state(snap)
            state_debug_capture("btn_improve_skills:staged")

            st.session_state["bullets_uses"] = st.session_state.get("bullets_uses", 0) + 1
            increment_usage(email_for_usage, "bullets_uses")

            st.success("AI skills applied.")
            state_debug_capture("btn_improve_skills:before_rerun")
            st.rerun()

        except Exception as e:
            restore_protected_state(snap)
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
_seen = set()
skills = [s for s in skills if not (s.lower() in _seen or _seen.add(s.lower()))]


# -------------------------
# 3. Experience (multiple roles)
# -------------------------
if st.session_state.get("_just_autofilled_from_cv", False):
    parsed_n = int(st.session_state.get("parsed_num_experiences", 1) or 1)
    st.session_state["num_experiences"] = max(1, min(5, parsed_n))
    st.session_state["_just_autofilled_from_cv"] = False  # ✅ consume it once

if "num_experiences" not in st.session_state or st.session_state["num_experiences"] is None:
    st.session_state["num_experiences"] = st.session_state.get("parsed_num_experiences", 1)

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

for i in range(int(num_experiences)):
    st.subheader(f"Role {i + 1}")

    job_title_key = f"job_title_{i}"
    company_key   = f"company_{i}"
    loc_key       = f"exp_location_{i}"
    start_key     = f"start_date_{i}"
    end_key       = f"end_date_{i}"
    desc_key      = f"description_{i}"

    safe_init_key(job_title_key, "")
    safe_init_key(company_key, "")
    safe_init_key(loc_key, "")
    safe_init_key(start_key, "")
    safe_init_key(end_key, "")
    safe_init_key(desc_key, "")

    apply_staged_value(desc_key)

    job_title = st.text_input("Job title", key=job_title_key)
    company   = st.text_input("Company", key=company_key)
    exp_loc   = st.text_input("Job location", key=loc_key)
    start_dt  = st.text_input("Start date (e.g. Jan 2020)", key=start_key)
    end_dt    = st.text_input("End date (e.g. Present or Jun 2023)", key=end_key)

    st.text_area(
        "Description / key achievements",
        key=desc_key,
        help="Use one bullet per line.",
    )

    if st.button("Improve this role (AI)", key=f"btn_role_ai_{i}"):
        state_debug_capture(f"btn_role_ai_{i}:clicked")
        if not gate_premium(f"improve Role {i+1} with AI"):
            st.stop()

        ok, left = cooldown_ok(f"improve_role_{i}", 5)
        if not ok:
            st.warning(f"⏳ Please wait {left}s before trying again.")
            st.stop()

        # ✅ SNAPSHOT right before doing anything that may rerun / error
        st.session_state["_snap_before_role_ai"] = snapshot_protected_state()

        st.session_state["ai_running_role"] = i
        st.session_state["ai_run_now"] = True
        state_debug_capture(f"btn_role_ai_{i}:before_rerun")
        st.rerun()

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

# ---------- Run AI AFTER render ----------
role_to_improve = st.session_state.get("ai_running_role")
run_now = st.session_state.pop("ai_run_now", False)

if run_now and role_to_improve is not None:
    i = int(role_to_improve)
    st.session_state["ai_running_role"] = None

    # ✅ If something clears, we can restore
    snap = st.session_state.pop("_snap_before_role_ai", None)

    desc_key = f"description_{i}"
    current_text = (st.session_state.get(desc_key) or "").strip()
    if not current_text:
        if snap: restore_protected_state(snap)
        st.warning("Please add text for this role first.")
        st.stop()

    email_for_usage = (st.session_state.get("user") or {}).get("email") or ""
    if not email_for_usage:
        if snap: restore_protected_state(snap)
        st.warning("Please sign in to use AI features.")
        st.stop()

    ok_spend = spend_ai_credit(email_for_usage, source=f"ai_role_improve_{i+1}", amount=1)
    if not ok_spend:
        if snap: restore_protected_state(snap)
        st.warning("You don’t have enough AI credits for this action.")
        st.stop()

    with st.spinner(f"Improving Role {i+1} description..."):
        try:
            improved = improve_bullets(current_text)
            improved_limited = enforce_word_limit(improved, MAX_DOC_WORDS, label=f"Role {i+1} description")

            stage_value(desc_key, improved_limited)
            state_debug_capture(f"role_ai_{i}:staged")
            if snap:
                restore_protected_state(snap)

            st.session_state["bullets_uses"] = st.session_state.get("bullets_uses", 0) + 1
            increment_usage(email_for_usage, "bullets_uses")

            st.success(f"Role {i+1} updated.")
            state_debug_capture(f"role_ai_{i}:before_rerun")
            st.rerun()

        except Exception as e:
            if snap: restore_protected_state(snap)
            st.error(f"AI error: {e}")

st.session_state.pop("_just_autofilled_from_cv", None)


# -------------------------
# 4. Education (multiple entries)
# -------------------------
st.header("4. Education (multiple entries)")

safe_init_key("num_education", 1)

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

    degree_key = f"degree_{i}"
    institution_key = f"institution_{i}"
    edu_location_key = f"edu_location_{i}"
    edu_start_key = f"edu_start_{i}"
    edu_end_key = f"edu_end_{i}"

    safe_init_key(degree_key, "")
    safe_init_key(institution_key, "")
    safe_init_key(edu_location_key, "")
    safe_init_key(edu_start_key, "")
    safe_init_key(edu_end_key, "")

    degree = st.text_input("Degree / qualification", key=degree_key)
    institution = st.text_input("Institution", key=institution_key)
    edu_location = st.text_input("Education location", key=edu_location_key)
    edu_start = st.text_input("Start date (e.g. Sep 2016)", key=edu_start_key)
    edu_end = st.text_input("End date (e.g. Jun 2019)", key=edu_end_key)

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

# ✅ store what you just built (instead of overwriting it with old state)
try:
    st.session_state["education_items"] = [e.dict() for e in education_items]
except Exception:
    st.session_state["education_items"] = [getattr(e, "__dict__", {}) for e in education_items]


# -------------------------
# 5. References (optional)
# -------------------------
st.header("5. References (optional)")
safe_init_key("references", "")

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
                state_debug_capture("adzuna_search:results_staged")

                if not jobs:
                    st.info("No results found. Try different keywords or a nearby location.")

                state_debug_capture("adzuna_search:before_rerun")
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
                            state_debug_capture("use_job:before_rerun")
                            st.rerun()

                st.markdown("**Preview description**")
                st.write(desc[:2500] + ("..." if len(desc) > 2500 else ""))


# -------------------------
# 6. Target Job (optional, for AI) — workspace safe:
# - DO NOT auto-pop outputs when JD changes
# - SNAPSHOT before AI actions
# -------------------------
st.header("5. Target Job (optional)")

def _fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8", errors="ignore")).hexdigest()

def get_personal_value(primary_key: str, fallback_key: str) -> str:
    return (st.session_state.get(primary_key) or st.session_state.get(fallback_key) or "").strip()

full_name_ss = get_personal_value("full_name", "cv_full_name")
email_ss     = get_personal_value("email", "cv_email")
title_ss     = get_personal_value("title", "cv_title")
phone_ss     = get_personal_value("phone", "cv_phone")
location_ss  = get_personal_value("location", "cv_location")

safe_init_key("job_description", "")
apply_staged_value("job_description")  # if you ever stage it

job_description = st.text_area(
    "Paste the job description here",
    height=200,
    help="Paste the full job spec from LinkedIn, Indeed, etc.",
    key="job_description",
)

# ✅ inline fingerprint (jd_fp was unused)
st.session_state["_last_jd_fp"] = _fingerprint(job_description)  # track it, but DO NOT clear other stuff

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
    state_debug_capture("btn_job_summary:clicked")
    snap = snapshot_protected_state("before_ai_job_summary")

    if not gate_premium("generate a job summary"):
        st.stop()

    if not (full_name_ss and email_ss):
        st.warning("Complete Section 1 (Full name + Email) first — these are used in outputs.")
        st.stop()

    if not job_description.strip():
        st.error("Please paste a job description first.")
        st.stop()

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

            stage_value("job_summary_ai", job_summary_text)
            restore_protected_state(snap)
            state_debug_capture("btn_job_summary:staged")
            st.session_state["job_summary_uses"] = st.session_state.get("job_summary_uses", 0) + 1

            if email_for_usage:
                increment_usage(email_for_usage, "job_summary_uses")

            st.success("AI job summary generated below.")
            state_debug_capture("btn_job_summary:before_rerun")
            st.rerun()

        except Exception as e:
            restore_protected_state(snap)
            st.error(f"AI error (job summary): {e}")
            st.stop()


# Display job summary
apply_staged_value("job_summary_ai")
job_summary_text = st.session_state.get("job_summary_ai", "")
if job_summary_text:
    st.markdown("**AI job summary for this role (read-only):**")
    st.write(job_summary_text)


# -------------------------
# AI cover letter generation
# -------------------------
if ai_cover_letter_clicked:
    state_debug_capture("btn_cover_letter:clicked")
    snap = snapshot_protected_state("before_ai_cover_letter")

    if not gate_premium("generate a cover letter"):
        st.stop()

    if not (full_name_ss and email_ss):
        st.warning("Complete Section 1 (Full name + Email) first — added to cover letter.")
        st.stop()

    if not job_description.strip():
        st.error("Please paste a job description first.")
        st.stop()

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
            # ✅ Keep education source consistent:
            # prefer local education_items (built this run), else session fallback
            edu_for_ai = education_items if "education_items" in locals() else st.session_state.get("education_items", [])

            cover_input = {
                "full_name": full_name_ss,
                "current_title": title_ss,
                "skills": skills,
                "experiences": [exp.dict() for exp in experiences],
                "education": edu_for_ai,
                "location": location_ss,
            }

            jd_limited = enforce_word_limit(job_description, MAX_DOC_WORDS, label="Job description (AI input)")
            job_summary = st.session_state.get("job_summary_ai", "") or ""

            cover_text = generate_cover_letter_ai(cover_input, jd_limited, job_summary)
            cleaned = clean_cover_letter_body(cover_text)
            final_letter = enforce_word_limit(cleaned, MAX_LETTER_WORDS, label="cover letter")

            stage_value("cover_letter", final_letter)
            stage_value("cover_letter_box", final_letter)
            restore_protected_state(snap)
            state_debug_capture("btn_cover_letter:staged")

            st.session_state["cover_uses"] = st.session_state.get("cover_uses", 0) + 1
            if email_for_usage:
                increment_usage(email_for_usage, "cover_uses")

            st.success("AI cover letter generated below. You can edit it before downloading.")
            state_debug_capture("btn_cover_letter:before_rerun")
            st.rerun()

        except Exception as e:
            restore_protected_state(snap)
            st.error(f"AI error (cover letter): {e}")
            st.stop()


# -------------------------
# Cover letter editor + downloads
# -------------------------
apply_staged_value("cover_letter")
apply_staged_value("cover_letter_box")
st.session_state.setdefault("cover_letter", "")

if st.session_state.get("cover_letter"):
    st.subheader("✏️ Cover letter")

    edited = st.text_area(
        "You can edit this before using it:",
        key="cover_letter_box",
        height=260,
    )
    st.session_state["cover_letter"] = edited

    try:
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

safe_init_key("template_label", "Blue")

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
    feature_label="generate and download your CV",
    key="btn_generate_cv",
)

if generate_clicked:
    state_debug_capture("btn_generate_cv:clicked")
    snapshot_protected_state("before_generate_cv")  # ✅ SNAPSHOT

    # clears only derived outputs (must be your SAFE version)
    clear_ai_upload_state_only()

    email_for_usage = (st.session_state.get("user") or {}).get("email")

    cv_full_name = get_cv_field("cv_full_name")
    cv_title     = get_cv_field("cv_title")
    cv_email     = get_cv_field("cv_email")
    cv_phone     = get_cv_field("cv_phone")
    cv_location  = get_cv_field("cv_location")
    raw_summary  = get_cv_field("cv_summary", "")

    if not cv_full_name or not cv_email:
        st.error("Please fill in at least your full name and email.")
        st.stop()

    if not email_for_usage:
        st.error("Please sign in again.")
        open_auth_modal("Sign in")
        st.stop()

    uid = get_user_id(email_for_usage)
    if not uid:
        st.error("Please sign in again.")
        st.stop()

    spent = try_spend(uid, source="cv_generate", cv=1)
    if not spent:
        st.warning("You don’t have enough CV credits to generate a CV.")
        st.stop()

    try:
        cv_summary = enforce_word_limit(raw_summary or "", MAX_DOC_WORDS, "Professional summary")

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

        template_name = TEMPLATE_MAP.get(st.session_state.get("template_label"), "Blue Theme.html")

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

# ✅ persistent checkout urls (survive reruns)
st.session_state.setdefault("checkout_url_monthly", None)
st.session_state.setdefault("checkout_url_pro", None)

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
            st.session_state["checkout_url_monthly"] = url
            state_debug_capture("start_monthly_sub:before_rerun")
            st.rerun()
        except Exception as e:
            st.error(f"Stripe error: {e}")

    # ✅ render link outside click handler so it persists
    if st.session_state.get("checkout_url_monthly"):
        st.link_button("Continue to secure checkout", st.session_state["checkout_url_monthly"])

with col_pro:
    st.subheader("Pro")
    st.markdown(
        "**£5.99 / month**\n\n"
        "- Monthly allowance: **50 CV + 90 AI**\n"
        "- PDF + Word downloads\n"
        "- Priority support\n"
        "- Cancel anytime\n"
        "\n"
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
            st.session_state["checkout_url_pro"] = url
            state_debug_capture("start_pro_sub:before_rerun")
            st.rerun()
        except Exception as e:
            st.error(f"Stripe error: {e}")

    # ✅ render link outside click handler so it persists
    if st.session_state.get("checkout_url_pro"):
        st.link_button("Continue to secure checkout", st.session_state["checkout_url_pro"])

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


state_debug_report("run:report")

# ==============================================
# FOOTER POLICY BUTTONS (MODAL ONLY - NO SNAPSHOT)
# ==============================================
st.markdown("<hr style='margin-top:40px;'>", unsafe_allow_html=True)

render_policy_modal("footer")

fc1, fc2, fc3, fc4 = st.columns(4)
with fc1:
    if st.button("Accessibility", key="footer_accessibility"):
        open_policy("footer", "accessibility")
with fc2:
    if st.button("Cookie Policy", key="footer_cookies"):
        open_policy("footer", "cookies")
with fc3:
    if st.button("Privacy Policy", key="footer_privacy"):
        open_policy("footer", "privacy")
with fc4:
    if st.button("Terms of Use", key="footer_terms"):
        open_policy("footer", "terms")
