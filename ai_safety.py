# ai_safety.py
import re

# ---------------------------------------------------------
# Word lists (expand anytime)
# ---------------------------------------------------------

SWEAR_WORDS = [
    "fuck", "f**k", "f*ck", "shit", "sh*t", "bitch", "b*tch",
    "bastard", "dick", "prick", "bollocks", "twat", "wanker"
]

HATE_SLURS = [
    "idiot", "stupid", "dumb", "moron", "retard"
]

ILLEGAL_CONTENT = [
    "kill", "stab", "murder", "bomb", "terror", "drug dealing"
]


# ---------------------------------------------------------
# Helper to highlight unsafe words
# ---------------------------------------------------------

def highlight(text):
    return f"**:yellow[{text}]**"


# ---------------------------------------------------------
# Main safety function
# ---------------------------------------------------------

def validate_and_clean(text: str):
    """
    Returns:
    - safe_text: cleaned version to be used
    - warning: string to show in Streamlit (or None)
    - action: "use", "illegal", "cleaned"
    """

    if not text.strip():
        return text, None, "use"

    lowered = text.lower()

    # 1) Illegal content
    for bad in ILLEGAL_CONTENT:
        if bad in lowered:
            w = f"⚠️ Your text contains illegal content ({highlight(bad)}). " \
                "This cannot be used in a CV. Please rewrite it."
            return text, w, "illegal"

    # 2) Swearing / hate speech
    found_swears = [w for w in SWEAR_WORDS if w in lowered]
    found_hate = [w for w in HATE_SLURS if w in lowered]

    if found_swears or found_hate:
        warning_parts = []

        if found_swears:
            sw = ", ".join(highlight(w) for w in found_swears)
            warning_parts.append(f"Swear words detected: {sw}")

        if found_hate:
            ht = ", ".join(highlight(w) for w in found_hate)
            warning_parts.append(f"Unprofessional wording: {ht}")

        warning = (
            "⚠️ Your text has been rewritten for professionalism.\n\n"
            + "\n".join(warning_parts)
        )

        # Rewrite the text (simple safe transform)
        safe = text
        for w in found_swears + found_hate:
            safe = re.sub(w, "unprofessional wording", safe, flags=re.IGNORECASE)

        return safe, warning, "cleaned"

    # No problems
    return text, None, "use"
