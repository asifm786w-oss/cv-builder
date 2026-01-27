import streamlit as st
import io
import csv
import os

import openai

from utils import verify_postgres_connection

verify_postgres_connection()


from openai import OpenAI
from auth import init_db
init_db()



from models import CV, Experience, Education
from utils import (
    render_cv_pdf_bytes,
    render_cover_letter_pdf_bytes,
    render_cv_docx_bytes,            # DOCX CV
    render_cover_letter_docx_bytes,  # DOCX Cover letter
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
import traceback

import streamlit as st
import time


# -------------------------
# GLOBAL PLAN + REFERRAL CONFIG
# -------------------------

REFERRAL_CAP = 10
BONUS_PER_REFERRAL_CV = 5
BONUS_PER_REFERRAL_AI = 5

PLAN_LIMITS = {
    "free": {"cv": 5, "ai": 5},

    # Public plans
    "monthly": {"cv": 20, "ai": 30},
    "pro": {"cv": 50, "ai": 90},

    # Legacy / optional
    "one_time": {"cv": 40, "ai": 60},
    "yearly": {"cv": 300, "ai": 600},

    # Internal only
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

def restore_skills_state():
    """
    If skills_text became None/blank after a rerun, restore from:
    1) last backup (most recent user/AI version)
    2) parsed CV (if available)
    """
    val = st.session_state.get("skills_text")

    if val is None or (isinstance(val, str) and not val.strip()):
        # 1) restore from backup (best)
        backup = st.session_state.get("_skills_backup")
        if isinstance(backup, str) and backup.strip():
            st.session_state["skills_text"] = backup
            return

        # 2) restore from parsed CV (fallback)
        parsed = st.session_state.get("_cv_parsed")
        if isinstance(parsed, dict):
            skills_data = parsed.get("skills")
            if isinstance(skills_data, list) and skills_data:
                st.session_state["skills_text"] = ", ".join([str(s).strip() for s in skills_data if str(s).strip()])
                return
            if isinstance(skills_data, str) and skills_data.strip():
                st.session_state["skills_text"] = skills_data.strip()
                return

        # 3) final fallback: empty (NOT "Python, SQL, Communication")
        st.session_state["skills_text"] = ""

def normalize_skills_state():
    # If skills got wiped to None (or missing), restore to safe value
    if st.session_state.get("skills_text") is None:
        st.session_state["skills_text"] = ""

    # If we have a parsed CV cached, restore from it when skills goes blank
    parsed = st.session_state.get("_cv_parsed")
    if isinstance(parsed, dict):
        skills_data = parsed.get("skills")
        if st.session_state.get("skills_text", "").strip() == "":
            if isinstance(skills_data, list) and skills_data:
                st.session_state["skills_text"] = ", ".join([s for s in skills_data if str(s).strip()])
            elif isinstance(skills_data, str) and skills_data.strip():
                st.session_state["skills_text"] = skills_data.strip()


def tripwire_none_experience_keys():
    keys = ["num_experiences", "job_title_0", "company_0", "description_0"]
    bad = {k: st.session_state.get(k) for k in keys if k in st.session_state and st.session_state.get(k) is None}
    if bad:
        st.sidebar.error(f"🚨 Experience keys set to None: {bad}")
        st.sidebar.code("".join(traceback.format_stack(limit=25)))

if st.session_state.get("debug_mode", False):
    tripwire_none_experience_keys()


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

    # Keep num_experiences consistent if it got lost/changed
    if st.session_state.get("num_experiences") in (None, 0, ""):
        st.session_state["num_experiences"] = count

    for i in range(count):
        exp = exps[i] or {}

        # Only restore if value is missing OR blank
        def _restore(key, value):
            if st.session_state.get(key) in (None, "") and isinstance(value, str) and value.strip():
                st.session_state[key] = value

        _restore(f"job_title_{i}",    exp.get("job_title", "") or "")
        _restore(f"company_{i}",      exp.get("company", "") or "")
        _restore(f"exp_location_{i}", exp.get("location", "") or "")
        _restore(f"start_date_{i}",   exp.get("start_date", "") or "")
        _restore(f"end_date_{i}",     exp.get("end_date", "") or "")
        desc = exp.get("description", "") or ""
        if isinstance(desc, list):
            desc = "\n".join([str(x) for x in desc if str(x).strip()])
        _restore(f"description_{i}",  desc)




def normalize_experience_state(max_roles: int = 5):
    # Fix count if it ever becomes None
    if st.session_state.get("num_experiences") is None:
        st.session_state["num_experiences"] = st.session_state.get("parsed_num_experiences", 1)

    # Fix any role fields that became None
    for i in range(max_roles):
        for k in ["job_title", "company", "exp_location", "start_date", "end_date", "description"]:
            key = f"{k}_{i}"
            if key in st.session_state and st.session_state[key] is None:
                st.session_state[key] = ""

        # Ensure keys exist so widgets never create them as None
        st.session_state.setdefault(f"job_title_{i}", "")
        st.session_state.setdefault(f"company_{i}", "")
        st.session_state.setdefault(f"exp_location_{i}", "")
        st.session_state.setdefault(f"start_date_{i}", "")
        st.session_state.setdefault(f"end_date_{i}", "")
        st.session_state.setdefault(f"description_{i}", "")

# -------------------------
# Word limit helpers
# -------------------------
MAX_PANEL_WORDS = 100      # e.g. free-text panels
MAX_DOC_WORDS = 300        # CV summary, job description, etc.
MAX_LETTER_WORDS = 300     # cover letter body limit


def limit_words(text: str, max_words: int) -> str:
    """Truncate text to at most `max_words` words."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def clean_cover_letter_body(text: str) -> str:
    cleaned_lines = []
    for line in text.splitlines():
        s = line.strip()
        # drop placeholder-like lines such as [Your Name], [Your Address]
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
    """
    Store the latest non-empty education fields so reruns can't wipe them.
    """
    edu_rows = []
    for i in range(max_rows):
        row = {
            "degree": st.session_state.get(f"degree_{i}", "") or "",
            "institution": st.session_state.get(f"institution_{i}", "") or "",
            "location": st.session_state.get(f"edu_location_{i}", "") or "",
            "start": st.session_state.get(f"edu_start_{i}", "") or "",
            "end": st.session_state.get(f"edu_end_{i}", "") or "",
        }
        # keep row if it has anything meaningful
        if any(v.strip() for v in row.values()):
            edu_rows.append(row)

    if edu_rows:
        st.session_state["_edu_backup"] = edu_rows


def restore_education_state(max_rows: int = 5):
    """
    If education keys became blank after a rerun, restore from backup.
    """
    backup = st.session_state.get("_edu_backup")
    if not isinstance(backup, list) or not backup:
        return

    # Only restore if current fields are empty-ish
    current_has_data = False
    for i in range(max_rows):
        if (st.session_state.get(f"degree_{i}", "") or "").strip() or (st.session_state.get(f"institution_{i}", "") or "").strip():
            current_has_data = True
            break

    if current_has_data:
        return  # don't overwrite real current data

    # Restore
    for i, row in enumerate(backup[:max_rows]):
        st.session_state[f"degree_{i}"] = row.get("degree", "")
        st.session_state[f"institution_{i}"] = row.get("institution", "")
        st.session_state[f"edu_location_{i}"] = row.get("location", "")
        st.session_state[f"edu_start_{i}"] = row.get("start", "")
        st.session_state[f"edu_end_{i}"] = row.get("end", "")


# -------------------------------------------------------------------
# OpenAI helpers (kept for future use if needed)
# -------------------------------------------------------------------
def _get_openai_client() -> OpenAI:
    """
    Try to construct an OpenAI client using:
    - OPENAI_API_KEY env var, or
    - several common keys in st.secrets, or
    - any secret that looks like an OpenAI key (starts with 'sk-')
    """
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        # Try some common secret names
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


# -------------------------
# Basic page config
# -------------------------
st.set_page_config(
    page_title="Modern CV Builder",
    page_icon="📄",
    layout="centered",
    initial_sidebar_state="expanded",
)
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

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
    background: radial-gradient(1200px 600px at 15% 10%, rgba(255,45,85,0.18), transparent 55%),
                radial-gradient(900px 500px at 80% 15%, rgba(255,59,48,0.12), transparent 55%),
                linear-gradient(180deg, #070a12 0%, var(--bg) 100%) !important;
    color: var(--text) !important;
    font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif !important;
}

[data-testid="stAppViewContainer"] .main .block-container{
    padding-top: 2rem;
    padding-bottom: 3rem;
    max-width: 920px;
}

/* ---------- Typography ---------- */
h1,h2,h3,h4,h5,h6{ color: var(--text) !important; letter-spacing: -0.02em; }
h1{ font-weight: 800 !important; }
h2{ font-weight: 750 !important; }
p, li { color: var(--text) !important; }

.stCaption, [data-testid="stCaptionContainer"]{
    color: var(--muted) !important;
}

/* Only style markdown text inside MAIN */
[data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] *{
    color: var(--text) !important;
}

/* ---------- Sidebar ---------- */
[data-testid="stSidebar"]{
    background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02)) !important;
    border-right: 1px solid rgba(255,255,255,0.10) !important;
}

/* ---------- Expanders ---------- */
[data-testid="stExpander"]{
    background: var(--panel) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
    box-shadow: var(--shadow-soft);
    overflow: hidden;
}

[data-testid="stExpander"] summary{
    padding: 0.7rem 0.9rem !important;
    font-weight: 650 !important;
    color: var(--text) !important;
}

/* FIX: Help expander body */
[data-testid="stSidebar"] [data-testid="stExpander"] div[role="region"]{
    background: rgba(0,0,0,0.32) !important;
    border-top: 1px solid rgba(255,255,255,0.10) !important;
    padding: 0.75rem 0.8rem !important;
    max-height: 240px !important;
    overflow-y: auto !important;
}

[data-testid="stSidebar"] [data-testid="stExpander"] div[role="region"] *{
    color: rgba(255,255,255,0.95) !important;
    opacity: 1 !important;
    visibility: visible !important;
    font-weight: 600 !important;
}

/* ---------- Inputs ---------- */
.stTextInput input,
.stNumberInput input,
.stDateInput input,
.stTextArea textarea,
div[data-baseweb="input"] input,
div[data-baseweb="textarea"] textarea{
    background: rgba(255,255,255,0.96) !important;
    color: #0b0f19 !important;
    caret-color: var(--red) !important;
    border: 1px solid rgba(0,0,0,0.08) !important;
    border-radius: var(--radius-sm) !important;
    font-size: 0.98rem !important;
    line-height: 1.45 !important;
}

.stTextArea textarea{ padding: 0.85rem 0.95rem !important; }
.stTextInput input, .stNumberInput input{ padding: 0.65rem 0.85rem !important; }

.stTextInput input::placeholder,
.stTextArea textarea::placeholder{
    color: rgba(11,15,25,0.45) !important;
}

/* ---------- Selectbox (input itself) ---------- */
div[data-baseweb="select"] > div{
    background: rgba(255,255,255,0.96) !important;
    color: #0b0f19 !important;
    border: 1px solid rgba(0,0,0,0.10) !important;
    border-radius: var(--radius-sm) !important;
    box-shadow: none !important;
    outline: none !important;
}

div[data-baseweb="select"] span{
    color: #0b0f19 !important;
    font-weight: 700 !important;
}

div[data-baseweb="select"] > div:focus,
div[data-baseweb="select"] > div:focus-within{
    border-color: rgba(255,45,85,0.35) !important;
    box-shadow: 0 0 0 3px rgba(255,45,85,0.10) !important;
    outline: none !important;
}

/* ==================================================
   DROPDOWN MENU (THE FIX YOU WANTED)
   dark menu + dark selected strip + readable text
   ================================================== */

div[data-baseweb="popover"] > div{
    background: rgba(8,10,16,0.98) !important;
    border: 1px solid rgba(255,255,255,0.14) !important;
    border-radius: 14px !important;
    box-shadow: 0 22px 60px rgba(0,0,0,0.65) !important;
    overflow: hidden !important;
}

div[data-baseweb="popover"] [role="listbox"]{
    max-height: 260px !important;
    overflow-y: auto !important;
    padding: 6px !important;
    background: transparent !important;
}

div[data-baseweb="popover"] [role="option"]{
    color: rgba(255,255,255,0.92) !important;
    background: transparent !important;
    padding: 10px 12px !important;
    border-radius: 10px !important;
}

div[data-baseweb="popover"] [role="option"]:hover{
    background: rgba(255,45,85,0.18) !important;
}

div[data-baseweb="popover"] [role="option"][aria-selected="true"]{
    background: rgba(255,45,85,0.28) !important;  /* darker than before */
    color: rgba(255,255,255,0.98) !important;
}

div[data-baseweb="popover"] *{
    opacity: 1 !important;
    filter: none !important;
}

/* ---------- Buttons ---------- */
.stButton button{
    border-radius: 999px !important;
    border: 1px solid rgba(255,255,255,0.14) !important;
    padding: 0.62rem 1.05rem !important;
    font-weight: 700 !important;
    background: rgba(255,255,255,0.06) !important;
    color: var(--text) !important;
    box-shadow: var(--shadow-soft);
    line-height: 1.2 !important;
    white-space: normal !important;
}

.stButton button:hover{
    border-color: rgba(255,45,85,0.55) !important;
    box-shadow: 0 0 0 4px rgba(255,45,85,0.10), var(--shadow);
    transform: translateY(-1px);
}

/* ---------- Download button ---------- */
[data-testid="stDownloadButton"] button{
    border-radius: 999px !important;
    border: 1px solid rgba(255,255,255,0.14) !important;
    background: rgba(255,255,255,0.06) !important;
    color: var(--text) !important;
    padding: 0.62rem 1.05rem !important;
    box-shadow: var(--shadow-soft) !important;
}

[data-testid="stDownloadButton"] button *{
    color: var(--text) !important;
    fill: var(--text) !important;
}

/* ---------- Inline code pills ---------- */
code{
    background: rgba(255,255,255,0.10) !important;
    color: rgba(255,255,255,0.92) !important;
    border: 1px solid rgba(255,255,255,0.14) !important;
    padding: 0.18rem 0.45rem !important;
    border-radius: 999px !important;
    font-size: 0.88rem !important;
}

/* ---------- File uploader ---------- */
[data-testid="stFileUploader"] section{
    background: rgba(255,255,255,0.06) !important;
    border: 1px solid rgba(255,255,255,0.14) !important;
    border-radius: 16px !important;
    box-shadow: var(--shadow-soft) !important;
    padding: 0.65rem 0.8rem !important;
    height: auto !important;
    min-height: unset !important;
}

[data-testid="stFileUploaderDropzone"]{
    padding: 0.55rem 0.7rem !important;
    min-height: 58px !important;
}

[data-testid="stFileUploader"] section *{
    color: rgba(255,255,255,0.92) !important;
}

/* Browse files button */
[data-testid="stFileUploader"] button{
    background: linear-gradient(90deg, var(--red) 0%, var(--red2) 100%) !important;
    border: 1px solid rgba(255,45,85,0.55) !important;
    color: #fff !important;
    border-radius: 999px !important;
    font-weight: 800 !important;
    padding: 0.55rem 1.05rem !important;
    box-shadow: 0 12px 35px rgba(255,45,85,0.22) !important;
}

[data-testid="stFileUploader"] button:hover{
    transform: translateY(-1px) !important;
    box-shadow: 0 0 0 4px rgba(255,45,85,0.12), 0 18px 50px rgba(0,0,0,0.35) !important;
}

/* ---------- Prevent BaseWeb weirdness ---------- */
div[data-baseweb] *{
    letter-spacing: normal !important;
    text-transform: none !important;
}

/* ---------- Remove Streamlit top chrome ---------- */
header[data-testid="stHeader"]{
    background: transparent !important;
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
/* Narrower sidebar so the CV builder dominates */
[data-testid="stSidebar"]{
  width: 280px !important;
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
/* Premium red edge glow on sidebar */
[data-testid="stSidebar"]{
  box-shadow: inset -1px 0 0 rgba(255,255,255,0.06),
              8px 0 30px rgba(255,45,85,0.10) !important;
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
/* Hide the random empty "pill" container that appears under sidebar headers */
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] > div:has(> div:empty) {
  display: none !important;
}

/* Extra safety: remove empty containers that Streamlit renders as bars */
[data-testid="stSidebar"] div:empty {
  display: none !important;
}
</style>
""", unsafe_allow_html=True)


# Hide Streamlit's default sidebar page list (multi-page navigation)
st.markdown(
    """
    <style>
        section[data-testid="stSidebarNav"] {
            display: none;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# =========================
# INIT
# =========================
init_db()

if "user" not in st.session_state:
    st.session_state["user"] = None

if "accepted_policies" not in st.session_state:
    st.session_state["accepted_policies"] = False

if "policy_view" not in st.session_state:
    st.session_state["policy_view"] = None  # None | cookies | privacy | terms | accessibility


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
        st.rerun()

    return True


# =========================
# CONSENT GATE (POST-LOGIN ONLY)
# =========================
def show_consent_gate():
    user = st.session_state.get("user")
    if not user:
        return

    email = user.get("email")

    if st.session_state.get("accepted_policies"):
        return

    try:
        if has_accepted_policies(email):
            st.session_state["accepted_policies"] = True
            return
    except Exception:
        pass

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
        if st.button("Cookie Policy"):
            st.session_state["policy_view"] = "cookies"
            st.rerun()
    with c2:
        if st.button("Privacy Policy"):
            st.session_state["policy_view"] = "privacy"
            st.rerun()
    with c3:
        if st.button("Terms of Use"):
            st.session_state["policy_view"] = "terms"
            st.rerun()

    agree = st.checkbox("I agree to the Cookie Policy, Privacy Policy and Terms of Use")

    if st.button("Accept and continue") and agree:
        try:
            mark_policies_accepted(email)
        except Exception:
            pass
        st.session_state["accepted_policies"] = True
        st.rerun()

    st.info("Please accept to continue using the site.")
    st.stop()


# =========================
# AUTH UI
# =========================
def auth_ui():
    """Login / register / password reset UI."""
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
            else:
                user = authenticate_user(login_email, login_password)
                if user:
                    st.session_state["user"] = user
                    st.success(f"Welcome back, {user.get('full_name') or user['email']}!")
                    st.rerun()
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
            elif reg_password != reg_password2:
                st.error("Passwords do not match.")
            else:
                referred_by_email = None

                if reg_referral_code.strip():
                    ref_user = get_user_by_referral_code(reg_referral_code.strip())
                    if not ref_user:
                        st.error("That referral code is not valid.")
                        st.stop()
                    referred_by_email = ref_user["email"]

                ok = create_user(reg_email, reg_password, reg_name, referred_by=referred_by_email)

                if ok:
                    if referred_by_email:
                        apply_referral_bonus(referred_by_email)

                    st.session_state["accepted_policies"] = False

                    user = authenticate_user(reg_email, reg_password)
                    if user:
                        st.session_state["user"] = user
                        st.rerun()
                    else:
                        st.success("Account created. Please sign in.")
                else:
                    st.error("That email is already registered.")

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
                    st.success("Password reset successfully. You can now sign in with your new password.")
                else:
                    st.error("Invalid or expired reset token. Please request a new reset link.")



# =========================
# ROUTING
# =========================
if show_policy_page():
    st.stop()

if st.session_state["user"] is None:
    auth_ui()
    st.stop()

show_consent_gate()


# -------------------------
# PUBLIC HOME / LANDING
# -------------------------
st.title("Munibs Career Support Tools")

st.write(
    "Build a modern CV and tailored cover letter in minutes. "
    "Use AI to improve your summary, bullets and cover letters, and "
    "download everything as PDF or Word."
)

st.markdown(
    """
    - ✅ Modern, clean CV templates  
    - 🤖 AI help for summaries, bullet points and cover letters  
    - 📄 Download as PDF and Word  
    - 🔐 Your data stays private to your account  
    """
)

# -------------------------
# AUTH GATE (login/register before consent gate)
# -------------------------
if st.session_state.get("user") is None:
    st.info("Create a free account or sign in to start using the tools.")
    auth_ui()
    st.stop()

# -------------------------
# CONSENT GATE (only after login)
# -------------------------
show_consent_gate()


# ✅ ENFORCE POLICIES HERE (MUST BE AFTER LOGIN, BEFORE APP LOADS)
show_consent_gate()

freeze_defaults()

# Logged-in user
current_user = st.session_state["user"]
user_email = current_user["email"]

# ---- Admin access: role-based ----
is_admin = current_user.get("role") in {"owner", "admin"}

# -------------------------
# Usage + plan configuration (DEFINE BEFORE SIDEBAR USES IT)
# -------------------------
REFERRAL_CAP = 10
BONUS_PER_REFERRAL_CV = 5
BONUS_PER_REFERRAL_AI = 5

PLAN_LIMITS = {
    "free": {"cv": 5, "ai": 5},

    # Public paid plans
    "monthly": {"cv": 20, "ai": 30},   # £2.99 Jobseeker Monthly
    "pro": {"cv": 50, "ai": 90},       # £5.99 Pro Monthly

    # Optional legacy plans
    "one_time": {"cv": 40, "ai": 60},
    "yearly": {"cv": 300, "ai": 600},

    # Internal-only (still metered)
    "premium": {"cv": 5000, "ai": 10000},
    "enterprise": {"cv": 5000, "ai": 10000},
}

AI_USAGE_KEYS = {"summary_uses", "cover_uses", "bullets_uses", "job_summary_uses"}
CV_USAGE_KEYS = {"cv_generations"}

USAGE_KEYS_DEFAULTS = {
    "upload_parses": 0,
    "summary_uses": 0,
    "cover_uses": 0,
    "bullets_uses": 0,
    "cv_generations": 0,
    "job_summary_uses": 0,
}

for k, default in USAGE_KEYS_DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = current_user.get(k, default)

# -------------------------
# Referral code (DEFINE BEFORE SIDEBAR USES IT)
# -------------------------
my_ref_code = current_user.get("referral_code")
if not my_ref_code:
    my_ref_code = ensure_referral_code(user_email)
    current_user["referral_code"] = my_ref_code
    st.session_state["user"]["referral_code"] = my_ref_code

my_ref_count = int(current_user.get("referrals_count", 0) or 0)
my_ref_count = min(my_ref_count, REFERRAL_CAP)

# -------------------------
# Mode select (ADMIN ONLY)
# -------------------------
if is_admin:
    mode = st.sidebar.radio("Mode", ["Use app", "Admin dashboard"], index=0, key="mode_select")
else:
    mode = "Use app"

def render_admin_dashboard():
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
                helper_count = sum(1 for u in users if u.get("role") == "helper" and u.get("email") != selected_email)
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
            if selected_email == current_user.get("email"):
                st.session_state["user"] = None
            st.rerun()

# Only render admin when selected
if mode == "Admin dashboard":
    render_admin_dashboard()
    st.stop()




# -------------------------
# Sidebar (ONE stable block)
# -------------------------
with st.sidebar:

    # ===== Mode (if you have it elsewhere, ignore this) =====
    # mode = st.radio("Mode", ["Use app", "Admin dashboard"], key="mode_sidebar")

    # -------------------------
    # Account Card (NO expander = no ghost)
    # -------------------------
    st.markdown("### 👤 Account")

    st.markdown(
        """
        <div style="
            background: rgba(255,255,255,0.06);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 16px;
            padding: 12px 14px;
            margin-bottom: 12px;
        ">
        """,
        unsafe_allow_html=True,
    )

    st.write(f"**Name:** {current_user.get('full_name') or '—'}")
    st.write(f"**Email:** {current_user.get('email')}")
    st.write(f"**Role:** `{current_user.get('role', 'user')}`")
    st.write(f"**Plan:** `{current_user.get('plan', 'free')}`")

    is_banned = current_user.get("is_banned", False)
    st.write(f"**Status:** {'🚫 Banned' if is_banned else '✅ Active'}")

    accepted_policies = current_user.get("accepted_policies", False)
    accepted_at = (current_user.get("accepted_policies_at") or "")[:19]
    st.write(
        f"**Policies accepted:** {'Yes' if accepted_policies else 'No'}"
        + (f" ({accepted_at})" if accepted_policies and accepted_at else "")
    )

    st.markdown("---")
    st.markdown("### 📊 Usage")

    role = current_user.get("role", "user")
    plan = current_user.get("plan", "free")
    referrals = min(current_user.get("referrals_count", 0) or 0, REFERRAL_CAP)

    plan_limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    base_cv = plan_limits["cv"]
    base_ai = plan_limits["ai"]

    if role in {"owner", "admin"}:
        base_cv = None
        base_ai = None

    used_cv = st.session_state.get("cv_generations", 0)
    used_ai_total = (
        st.session_state.get("summary_uses", 0)
        + st.session_state.get("cover_uses", 0)
        + st.session_state.get("bullets_uses", 0)
        + st.session_state.get("job_summary_uses", 0)
    )

    bonus_cv = referrals * BONUS_PER_REFERRAL_CV
    bonus_ai = referrals * BONUS_PER_REFERRAL_AI

    if base_cv is None:
        st.write("**CV Generations:** ♾️ Unlimited")
    else:
        remaining_cv = max(base_cv + bonus_cv - used_cv, 0)
        st.write(f"**CV Remaining:** {remaining_cv}")

    if base_ai is None:
        st.write("**AI Tools:** ♾️ Unlimited")
    else:
        remaining_ai = max(base_ai + bonus_ai - used_ai_total, 0)
        st.write(f"**AI Remaining:** {remaining_ai}")

    st.markdown("</div>", unsafe_allow_html=True)

    if st.button("Log out"):
        st.session_state["user"] = None
        st.rerun()

    # -------------------------
    # Referral Program
    # -------------------------
    st.markdown("### 🔗 Referral Program")
    st.write(f"**Your code:** `{my_ref_code}`")
    st.write(f"**Referrals used:** {my_ref_count} / {REFERRAL_CAP}")

    st.markdown("---")

    # -------------------------
    # Help Card (NO expander = no ghost)
    # -------------------------
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
        label_visibility="visible",
    )

    st.markdown(
        """
        <div style="
            background: rgba(255,255,255,0.06);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 16px;
            padding: 12px 14px;
            margin-top: 8px;
            max-height: 260px;
            overflow-y: auto;
        ">
        """,
        unsafe_allow_html=True,
    )

    if help_topic == "Quick Start":
        st.markdown(
            """
**Quick start**
1) Fill **Personal Details**  
2) Add **Skills**  
3) Add **Experience** roles  
4) Add **Education**  
5) (Optional) paste a **Job Description**  
6) Generate and download your CV as **PDF** or **Word**
            """
        )

    elif help_topic == "AI Tools & Usage":
        st.markdown(
            """
**What the AI tools do**
- Improve your professional summary
- Rewrite bullet points for clarity and impact
- Generate tailored cover letters
- Help structure content from uploaded CVs

**Usage limits**
Each plan includes a monthly allowance of AI actions and CV generations.
Usage resets automatically each month.

**Cooldown**
AI buttons have a short cooldown to prevent accidental double clicks.
            """
        )

    elif help_topic == "Cover Letter Rules":
        st.markdown(
            """
**Before generating a cover letter**
Make sure **Personal Details** are completed.

Why?
- Your name and contact details are used in the letter header
- The letter ends with a proper sign-off (*Regards, Your Name*)
            """
        )

    elif help_topic == "Templates & Downloads":
        st.markdown(
            """
**Templates**
Templates control the **visual style** of your CV (mainly the PDF).
Your content stays the same.

**PDF vs Word**
- **PDF**: best for applications
- **Word (.docx)**: best for editing
            """
        )

    elif help_topic == "Troubleshooting":
        st.markdown(
            """
**AI appears busy**
Wait 10–30 seconds and try again once.

**Text reset or disappeared**
Use a single browser tab.

**Formatting issues**
Use shorter bullets, consistent dates, or try a different template.
            """
        )

    elif help_topic == "Privacy & Refunds":
        st.markdown(
            """
**Privacy**
Only upload information you actually need.

**Refunds**
Payments are **non-refundable once used**.
If you believe there was a billing error, contact support.
            """
        )

    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("**Support:** support@affiliateworldcommissions.com")



def has_free_quota(counter_key: str, limit: int, feature_label: str) -> bool:
    global current_user

    # Admins / owners are unlimited
    if current_user.get("role") in {"owner", "admin"}:
        return True

    # Upload parsing is free onboarding
    if counter_key == "upload_parses":
        return True

    plan = current_user.get("plan", "free")
    plan_limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])

    if counter_key in CV_USAGE_KEYS:
        base_limit = plan_limits["cv"]
        bucket_keys = CV_USAGE_KEYS
    else:
        base_limit = plan_limits["ai"]
        bucket_keys = AI_USAGE_KEYS

    if base_limit is None:
        return True

    referrals = min(current_user.get("referrals_count", 0) or 0, REFERRAL_CAP)

    bonus = (
        referrals * BONUS_PER_REFERRAL_CV
        if counter_key in CV_USAGE_KEYS
        else referrals * BONUS_PER_REFERRAL_AI
    )

    effective_limit = base_limit + bonus
    used = sum(st.session_state.get(k, 0) for k in bucket_keys)

    if used >= effective_limit:
        st.warning(f"Free limit reached for {feature_label}. Upgrade or use referrals.")
        return False

    return True

# -------------------------
# Helper: paywall + quota check
# -------------------------
def show_paywall(feature_label: str):
    st.markdown(
        f"""
        <div style="
            border-radius: 12px;
            padding: 14px 16px;
            margin: 10px 0 4px 0;
            background: #eff6ff;
            border: 1px solid #bfdbfe;
        ">
            <div style="font-weight: 600; margin-bottom: 4px; color: #1d4ed8;">
                Free limit reached for {feature_label}.
            </div>
            <div style="font-size: 12px; color: #1f2937; margin-bottom: 8px;">
                Upgrade your plan or use referrals to unlock more usage.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )



# -------------------------
# FROM HERE DOWN: main app logic
# -------------------------
st.title("📄 Modern CV Builder")

st.write(
    "Fill in your details below and generate a clean, modern CV as a PDF. "
    "You can also tailor your CV and generate a cover letter for a specific job using AI."
)

def reset_generated_outputs_on_new_cv():
    # Anything derived from the previous CV / job should be cleared
    for k in [
        "cover_letter",
        "cover_letter_box",
        "summary_ai",
        "job_description",
    ]:
        st.session_state.pop(k, None)

    # Also clear per-role AI bullet outputs if you store them
    for i in range(5):
        st.session_state.pop(f"description_ai_{i}", None)
        st.session_state.pop(f"use_ai_{i}", None)

# -------------------------
# 0. Upload existing CV (optional)
# -------------------------
import hashlib

st.subheader("Upload an existing CV (optional)")

# Optional deps
try:
    from PyPDF2 import PdfReader
except ImportError:
    PdfReader = None

try:
    import docx2txt
except ImportError:
    docx2txt = None


def _read_uploaded_cv_to_text(uploaded_file) -> str:
    """Return extracted text from PDF/DOCX/TXT. Never raises; returns '' on failure."""
    if not uploaded_file:
        return ""

    name = (uploaded_file.name or "").lower()

    # Rewind to be safe (Streamlit can re-use the same file object on reruns)
    try:
        uploaded_file.seek(0)
    except Exception:
        pass

    # PDF
    if name.endswith(".pdf"):
        if PdfReader is None:
            st.error("PDF support not installed. Add PyPDF2 to requirements.")
            return ""
        try:
            reader = PdfReader(uploaded_file)
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as e:
            st.error(f"Could not read PDF: {e}")
            return ""

    # DOCX
    if name.endswith(".docx"):
        if docx2txt is None:
            st.error("DOCX support not installed. Add docx2txt to requirements.")
            return ""
        try:
            return docx2txt.process(uploaded_file) or ""
        except Exception as e:
            st.error(f"Could not read DOCX: {e}")
            return ""

    # TXT
    try:
        return uploaded_file.read().decode("utf-8", errors="ignore")
    except Exception as e:
        st.error(f"Could not read file: {e}")
        return ""


def _reset_outputs_on_new_cv():
    """Clear anything derived from old CV/job so it doesn't leak into the next one."""
    for k in [
        "cover_letter",
        "cover_letter_box",
        "summary_ai",
        "job_description",
    ]:
        st.session_state.pop(k, None)

    # Clear per-role AI bullet outputs if you use them
    for i in range(5):
        st.session_state.pop(f"description_ai_{i}", None)
        st.session_state.pop(f"use_ai_{i}", None)

import os
import logging

logging.warning("OPENAI_API_KEY present? %s", bool(os.getenv("OPENAI_API_KEY")))
def _apply_parsed_cv_to_session(parsed: dict) -> None:
    """Apply parsed CV dict to Streamlit session_state. Safe defaults + cleanup."""

    # --- Basic fields ---
    st.session_state["full_name"] = parsed.get("full_name", "") or ""
    st.session_state["title"] = parsed.get("title", "") or ""
    st.session_state["email"] = parsed.get("email", "") or ""
    st.session_state["phone"] = parsed.get("phone", "") or ""
    st.session_state["location"] = parsed.get("location", "") or ""
    st.session_state["summary"] = parsed.get("summary", "") or ""

    # --- Skills ---
    skills_data = parsed.get("skills", [])
    if isinstance(skills_data, list):
        st.session_state["skills_text"] = ", ".join([str(s).strip() for s in skills_data if str(s).strip()])
    elif isinstance(skills_data, str):
        st.session_state["skills_text"] = skills_data

    # --- Experiences ---
    exps = parsed.get("experiences", []) or []
    if isinstance(exps, list) and exps:
        count = min(len(exps), 5)
        st.session_state["num_experiences"] = count

        for i in range(count):
            exp = exps[i] or {}
            st.session_state[f"job_title_{i}"] = exp.get("job_title", "") or ""
            st.session_state[f"company_{i}"] = exp.get("company", "") or ""
            st.session_state[f"exp_location_{i}"] = exp.get("location", "") or ""
            st.session_state[f"start_date_{i}"] = exp.get("start_date", "") or ""
            st.session_state[f"end_date_{i}"] = exp.get("end_date", "") or ""
            st.session_state[f"description_{i}"] = exp.get("description", "") or ""

        # Cleanup extra roles from previous CV
        for j in range(count, 5):
            for k in ["job_title", "company", "exp_location", "start_date", "end_date", "description"]:
                st.session_state.pop(f"{k}_{j}", None)

    else:
        st.session_state["num_experiences"] = 1

    # --- Education (map up to 5) ---
    edus = parsed.get("education", []) or []
    if isinstance(edus, list) and edus:
        edu_count = min(len(edus), 5)
        st.session_state["num_education"] = edu_count

        for i in range(edu_count):
            edu = edus[i] or {}
            st.session_state[f"degree_{i}"] = edu.get("degree", "") or ""
            st.session_state[f"institution_{i}"] = edu.get("institution", "") or ""
            st.session_state[f"edu_location_{i}"] = edu.get("location", "") or ""
            st.session_state[f"edu_start_{i}"] = edu.get("start_date", "") or ""
            st.session_state[f"edu_end_{i}"] = edu.get("end_date", "") or ""

        # Cleanup extra education rows from previous CV
        for j in range(edu_count, 5):
            for k in ["degree", "institution", "edu_location", "edu_start", "edu_end"]:
                st.session_state.pop(f"{k}_{j}", None)
    else:
        st.session_state["num_education"] = 1


uploaded_cv = st.file_uploader(
    "Upload your current CV (PDF, DOCX or TXT)",
    type=["pdf", "docx", "txt"],
    key="cv_uploader",
)

fill_clicked = st.button("Fill the form from this CV (AI)", key="btn_fill_from_cv")

if uploaded_cv is not None and fill_clicked:
    # upload_parses is not limited in your logic, but keep the call anyway
    if not has_free_quota("upload_parses", 1, "CV upload & parsing"):
        st.stop()

    raw_text = _read_uploaded_cv_to_text(uploaded_cv)
    if not raw_text.strip():
        st.warning("No readable text found in that file.")
        st.stop()

    # Fingerprint CV content so we reset only when it's a NEW CV
    cv_fp = hashlib.sha256(raw_text.encode("utf-8", errors="ignore")).hexdigest()
    last_fp = st.session_state.get("_last_cv_fingerprint")

    with st.spinner("Reading and analysing your CV..."):
        try:
            parsed = extract_cv_data(raw_text)
            if not isinstance(parsed, dict):
                raise ValueError("extract_cv_data() did not return a dict")

            if cv_fp != last_fp:
                _reset_outputs_on_new_cv()
                st.session_state["_last_cv_fingerprint"] = cv_fp

            _apply_parsed_cv_to_session(parsed)

            st.session_state["_cv_parsed"] = parsed
            st.session_state["_cv_autofill_enabled"] = True

            st.success("Form fields updated from your CV. Scroll down to review and edit.")
            st.session_state["upload_parses"] = st.session_state.get("upload_parses", 0) + 1
            increment_usage(user_email, "upload_parses")

            st.rerun()

        except Exception as e:
            import logging, traceback
            logging.error("CV PARSE FAILED: %s: %r", type(e).__name__, e)
            logging.error(traceback.format_exc())
            st.error(f"AI error while parsing CV: {e}")





restore_skills_state()
backup_skills_state()
# -------------------------
# 1. Personal details
# -------------------------
st.header("1. Personal details")

full_name = st.text_input("Full name *", key="full_name")
title = st.text_input("Professional title (e.g. Software Engineer)", key="title")
email = st.text_input("Email *", key="email")
phone = st.text_input("Phone", key="phone")

location = st.text_input("Location (City, Country)", key="location")

# --- Apply staged summary BEFORE widget renders ---
if "summary_pending" in st.session_state:
    st.session_state["summary"] = st.session_state.pop("summary_pending")

summary_text = st.text_area("Professional summary", height=120, key="summary")
st.caption(f"Tip: keep this under {MAX_PANEL_WORDS} words – extra text will be ignored.")

btn_summary = st.button(
    "Improve professional summary (AI)",
    key="btn_improve_summary",
)

if btn_summary:
    ok, left = cooldown_ok("improve_summary", 5)

    if not ok:
        st.warning(f"⏳ Please wait {left}s before trying again.")
    else:
        if not summary_text.strip():
            st.error("Please write a professional summary first.")
        elif not has_free_quota("summary_uses", 1, "AI professional summary"):
            pass
        else:
            with st.spinner("Improving your professional summary..."):
                try:
                    cv_like = {
                        "full_name": full_name,
                        "current_title": title,
                        "location": location,
                        "existing_summary": summary_text,
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
                    st.session_state["summary_pending"] = improved_limited

                    st.session_state["summary_uses"] = st.session_state.get("summary_uses", 0) + 1
                    increment_usage(user_email, "summary_uses")

                    st.success("AI summary applied into your main box.")
                    st.rerun()

                except Exception as e:
                    st.error(f"AI error (summary improvement): {e}")



normalize_skills_state()
st.header("2. Skills")

# -------------------------
# 2. Skills
# -------------------------

# ✅ Apply staged AI value BEFORE widget renders
if "skills_pending" in st.session_state:
    st.session_state["skills_text"] = st.session_state.pop("skills_pending")

# ✅ Default only if missing or None (never overwrite CV)
if "skills_text" not in st.session_state or st.session_state["skills_text"] is None:
    st.session_state["skills_text"] = "Python, SQL, Communication"

skills_text = st.text_area(
    "Skills (comma separated)",
    key="skills_text",
    help="For example: Python, SQL, Leadership, Problem-solving",
)

btn_skills = st.button("Improve skills (AI)", key="btn_improve_skills")

if btn_skills:
    ok, left = cooldown_ok("improve_skills", 5)
    if not ok:
        st.warning(f"⏳ Please wait {left}s before trying again.")
        st.stop()

    if not skills_text.strip():
        st.warning("Please add some skills first.")
    elif not has_free_quota("bullets_uses", 1, "AI skills improvement"):
        pass
    else:
        with st.spinner("Improving your skills..."):
            try:
                improved = improve_bullets(skills_text)

                # compress to comma-separated
                improved_clean = ", ".join(
                    line.strip("•- \t")
                    for line in improved.splitlines()
                    if line.strip()
                )

                improved_limited = enforce_word_limit(
                    improved_clean,
                    MAX_DOC_WORDS,
                    label="Skills (AI)",
                )

                # ✅ Stage for NEXT run (never mutate widget key now)
                st.session_state["skills_pending"] = improved_limited

                st.session_state["bullets_uses"] = st.session_state.get("bullets_uses", 0) + 1
                increment_usage(user_email, "bullets_uses")

                st.success("AI skills applied.")
                st.rerun()

            except Exception as e:
                st.error(f"AI error (skills improvement): {e}")

# Build skills list ONCE for downstream use
skills = [
    s.strip()
    for s in (st.session_state.get("skills_text") or "").replace("\n", ",").split(",")
    if s.strip()
]


restore_experience_from_parsed()
st.header("3. Experience (multiple roles)")

# -------------------------
# 3. Experience (multiple roles)
# -------------------------


# Keep count stable (parsed -> UI)
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

# ---------- Run AI AFTER the loop ----------
role_to_improve = st.session_state.get("ai_running_role")
run_now = st.session_state.get("ai_run_now", False)

if run_now and role_to_improve is not None:
    i = int(role_to_improve)

    # reset flags first so it doesn't loop
    st.session_state["ai_running_role"] = None
    st.session_state["ai_run_now"] = False

    desc_key    = f"description_{i}"
    pending_key = f"description_pending_{i}"
    current_text = (st.session_state.get(desc_key) or "").strip()

    if not current_text:
        st.warning("Please add text for this role first.")
    elif not has_free_quota("bullets_uses", 1, "role description improvements"):
        pass
    else:
        with st.spinner(f"Improving Role {i+1} description..."):
            try:
                improved = improve_bullets(current_text)
                improved_limited = enforce_word_limit(
                    improved,
                    MAX_DOC_WORDS,
                    label=f"Role {i+1} description",
                )

                # ✅ stage update for next rerun
                st.session_state[pending_key] = improved_limited

                st.session_state["bullets_uses"] = st.session_state.get("bullets_uses", 0) + 1
                increment_usage(user_email, "bullets_uses")

                st.success(f"Role {i+1} updated.")
                st.rerun()

            except Exception as e:
                st.error(f"AI error: {e}")


# ---------- Run AI AFTER the loop ----------
role_to_improve = st.session_state.get("ai_running_role")
run_now = st.session_state.pop("ai_run_now", False)

if run_now and role_to_improve is not None:
    i = int(role_to_improve)
    desc_key    = f"description_{i}"
    pending_key = f"description_pending_{i}"
    current_text = (st.session_state.get(desc_key) or "").strip()

    if not current_text:
        st.warning("Please add text for this role first.")
    elif not has_free_quota("bullets_uses", 1, "role description improvements"):
        pass
    else:
        with st.spinner(f"Improving Role {i+1} description..."):
            try:
                improved = improve_bullets(current_text)
                improved_limited = enforce_word_limit(
                    improved,
                    MAX_DOC_WORDS,
                    label=f"Role {i+1} description",
                )

                st.session_state[pending_key] = improved_limited
                st.session_state["bullets_uses"] = st.session_state.get("bullets_uses", 0) + 1
                increment_usage(user_email, "bullets_uses")

                st.session_state["ai_running_role"] = None
                st.rerun()

            except Exception as e:
                st.error(f"AI error: {e}")

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
# 5. Target Job (optional, for AI)
# -------------------------
st.header("5. Target Job (optional)")

job_description = st.text_area(
    "Paste the job description here",
    height=200,
    help="Paste the full job spec from LinkedIn, Indeed, etc.",
    key="job_description",
)

import hashlib

def _fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8", errors="ignore")).hexdigest()

jd_fp = _fingerprint(job_description)
last_jd_fp = st.session_state.get("_last_jd_fp")

if last_jd_fp and jd_fp != last_jd_fp:
    # Job changed: clear anything derived from it
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


# --- AI job-description summary (separate from professional summary) ---
if job_summary_clicked:
    # ✅ Guard: must complete Section 1 first
    if not (full_name.strip() and email.strip()):
        st.warning(
            "Complete Section 1 (Full name + Email) first — these are automatically used in your outputs."
        )
        st.stop()

    # ✅ Guard: need a job description
    if not job_description.strip():
        st.error("Please paste a job description first.")
        st.stop()

    # ✅ Quota check
    if not has_free_quota("job_summary_uses", 1, "AI job summary"):
        pass
    else:
        with st.spinner("Generating AI job summary..."):
            try:
                jd_limited = enforce_word_limit(
                    job_description,
                    MAX_DOC_WORDS,
                    label="Job description",
                )

                job_summary_text = generate_job_summary(jd_limited)

                st.session_state["job_summary_ai"] = job_summary_text
                st.session_state["job_summary_uses"] = (
                    st.session_state.get("job_summary_uses", 0) + 1
                )
                increment_usage(user_email, "job_summary_uses")

                st.success(
                    "AI job summary generated below. "
                    "You can copy it into applications or your notes."
                )
            except Exception as e:
                st.error(f"AI error (job summary): {e}")

job_summary_text = st.session_state.get("job_summary_ai", "")
if job_summary_text:
    st.markdown("**AI job summary for this role (read-only):**")
    st.write(job_summary_text)


# --- AI cover letter ---
# ✅ JD fingerprint clearing: if JD changes, clear derived outputs
import hashlib

def _fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8", errors="ignore")).hexdigest()

# Use the actual current text-area value (job_description)
jd_fp = _fingerprint(job_description)
last_jd_fp = st.session_state.get("_last_jd_fp")

if last_jd_fp and jd_fp != last_jd_fp:
    st.session_state.pop("job_summary_ai", None)
    st.session_state.pop("cover_letter", None)
    st.session_state.pop("cover_letter_box", None)

st.session_state["_last_jd_fp"] = jd_fp


# --- AI cover letter ---
if ai_cover_letter_clicked:
    # ✅ Guard: must complete Section 1 first
    if not (full_name.strip() and email.strip()):
        st.warning(
            "Complete Section 1 (Full name + Email) first — these are automatically added to your cover letter."
        )
        st.stop()

    # ✅ Guard: need a job description
    if not job_description.strip():
        st.error("Please paste a job description first.")
        st.stop()

    # ✅ Quota check
    if not has_free_quota("cover_uses", 1, "AI cover letter"):
        pass
    else:
        with st.spinner("Generating cover letter..."):
            cover_input = {
                "full_name": full_name,
                "current_title": title,
                "skills": skills,
                "experiences": [exp.dict() for exp in experiences],
                "education": st.session_state.get("education_items", []),
                "location": location,
            }

            # ✅ Always enforce JD limit for AI input (and keep it consistent)
            jd_limited = enforce_word_limit(
                job_description,
                MAX_DOC_WORDS,
                label="Job description (AI input)",
            )

            try:
                job_summary = st.session_state.get("job_summary_ai", "") or ""

                cover_text = generate_cover_letter_ai(
                    cover_input,
                    jd_limited,
                    job_summary,
                )

                cleaned = clean_cover_letter_body(cover_text)

                final_letter = enforce_word_limit(
                    cleaned,
                    MAX_LETTER_WORDS,
                    label="cover letter",
                )

                st.session_state["cover_letter"] = final_letter
                st.session_state["cover_uses"] = st.session_state.get("cover_uses", 0) + 1
                increment_usage(user_email, "cover_uses")

                st.success(
                    "AI cover letter generated below. "
                    "You can edit it before downloading."
                )

            except Exception as e:
                st.error(f"AI error (cover letter): {e}")


if "cover_letter" not in st.session_state:
    st.session_state["cover_letter"] = ""

if st.session_state["cover_letter"]:
    st.subheader("✏️ Cover letter")
    edited = st.text_area(
        "You can edit this before using it:",
        value=st.session_state["cover_letter"],
        height=260,
        key="cover_letter_box",
    )
    st.session_state["cover_letter"] = edited

    try:
        letter_pdf = render_cover_letter_pdf_bytes(
            full_name=full_name or "Candidate",
            letter_body=st.session_state["cover_letter"],
            location=location or "",
            email=email or "",
            phone=phone or "",
        )

        letter_docx = render_cover_letter_docx_bytes(
            full_name=full_name or "Candidate",
            letter_body=st.session_state["cover_letter"],
            location=location or "",
            email=email or "",
            phone=phone or "",
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
                mime=(
                    "application/vnd.openxmlformats-"
                    "officedocument.wordprocessingml.document"
                ),
            )

    except Exception as e:
        st.error(f"Error generating cover letter files: {e!r}")





# -------------------------
# 6. References (optional)
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


# -------------------------
# 7. Generate CV
# -------------------------

# -------------------------
# Template selection
# -------------------------
st.header("CV Template")

# keep a stable default
st.session_state.setdefault("template_label", "Blue")

template_label = st.selectbox(
    "Choose a CV template",
    options=list(TEMPLATE_MAP.keys()),
    index=list(TEMPLATE_MAP.keys()).index(st.session_state["template_label"])
    if st.session_state["template_label"] in TEMPLATE_MAP else 0,
    key="template_label",
)

if st.button("Generate CV (PDF + Word)"):
    if not full_name or not email:
        st.error("Please fill in at least your full name and email.")
    else:
        if not has_free_quota("cv_generations", 1, "CV generation"):
            # paywall already shown inside has_free_quota
            pass
        else:
            try:
                # 🔹 Use whatever is currently in the summary box
                #    (already cleaned / AI-improved if the button was used)
                raw_summary = st.session_state.get("summary", "") or ""

                # Enforce word limit on the summary before putting it in the CV
                cv_summary = enforce_word_limit(
                    raw_summary,
                    MAX_DOC_WORDS,
                    "Professional summary",
                )

                cv = CV(
                    full_name=full_name,
                    title=title or None,
                    email=email,
                    phone=phone or None,
                    full_address=None,
                    location=location or None,
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


                pdf_bytes = render_cv_pdf_bytes(
                cv,
                template_name=template_name,
                )                              
                docx_bytes = render_cv_docx_bytes(cv)

                st.success("CV generated successfully! 🎉")

                col_cv1, col_cv2 = st.columns(2)
                with col_cv1:
                    st.download_button(
                        label="📄 Download CV as PDF",
                        data=pdf_bytes,
                        file_name="cv.pdf",
                        mime="application/pdf",
                    )
                with col_cv2:
                    st.download_button(
                        label="📝 Download CV as Word (.docx)",
                        data=docx_bytes,
                        file_name="cv.docx",
                        mime=(
                            "application/vnd.openxmlformats-"
                            "officedocument.wordprocessingml.document"
                        ),
                    )

                # Count this as a CV generation
                st.session_state["cv_generations"] = (
                    st.session_state.get("cv_generations", 0) + 1
                )
                increment_usage(user_email, "cv_generations")

            except Exception as e:
                st.error(f"Something went wrong while generating the CV: {e}")


# -------------------------
# Pricing
# -------------------------
st.header("Pricing")

col_free, col_job, col_pro = st.columns(3)

with col_free:
    st.subheader("Free")
    st.markdown(
        "**£0**\n\n"
        "- **4 CV generations**\n"
        "- **6 AI actions** (summary / cover / bullets / upload parsing)\n"
        "- Includes free templates\n"
        "- Referral program: every successful referral gives you **+5 CVs** and "
        "**+5 AI actions** (up to 10 friends)\n"
    )

with col_job:
    st.subheader("Jobseeker Monthly")
    st.markdown(
        "**£2.99 / month**\n\n"
        "- **20 CV generations / month**\n"
        "- **30 AI actions / month**\n"
        "- Access to all CV templates\n"
        "- PDF + Word downloads\n"
        "- Email support\n"
    )

with col_pro:
    st.subheader("Pro Monthly")
    st.markdown(
        "**£5.99 / month**\n\n"
        "- **50 CV generations / month**\n"
        "- **90 AI actions / month**\n"
        "- Access to all CV templates\n"
        "- PDF + Word downloads\n"
        "- Priority support\n"
    )

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
    "All plans are metered to keep the service reliable and prevent abuse. "
    "If you're running a programme (council/charity/organisation), ask about Enterprise licensing."
)

st.markdown("<hr style='margin-top:40px;'>", unsafe_allow_html=True)

fc1, fc2, fc3, fc4 = st.columns(4)
with fc1:
    if st.button("Accessibility", key="footer_accessibility"):
        st.session_state["policy_view"] = "accessibility"
        st.rerun()
with fc2:
    if st.button("Cookie Policy", key="footer_cookies"):
        st.session_state["policy_view"] = "cookies"
        st.rerun()
with fc3:
    if st.button("Privacy Policy", key="footer_privacy"):
        st.session_state["policy_view"] = "privacy"
        st.rerun()
with fc4:
    if st.button("Terms of Use", key="footer_terms"):
        st.session_state["policy_view"] = "terms"
        st.rerun()


