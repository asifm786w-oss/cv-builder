# ai_v2.py
import os
import json
import streamlit as st
from openai import OpenAI

# -------------------------------------------------------------------
# Internal: get OpenAI client from env or Streamlit secrets
# -------------------------------------------------------------------
def _get_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        if "OPENAI_API_KEY" in st.secrets:
            api_key = st.secrets["OPENAI_API_KEY"]
        elif "openai_api_key" in st.secrets:
            api_key = st.secrets["openai_api_key"]
        elif "openai" in st.secrets and isinstance(st.secrets["openai"], dict):
            section = st.secrets["openai"]
            if "api_key" in section:
                api_key = section["api_key"]

    if not api_key:
        raise RuntimeError("No OpenAI API key found. Set OPENAI_API_KEY or Streamlit secrets.")

    api_key = api_key.strip()
    return OpenAI(api_key=api_key, timeout=60.0, max_retries=2)


# -------------------------------------------------------------------
# Keep your cleaner (but now we DO NOT ask the model to output "Dear ...")
# -------------------------------------------------------------------
def _clean_cover_letter_body(text: str) -> str:
    if not text:
        return ""

    lines = text.splitlines()
    cleaned_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("dear "):  # safety, but model won't output greetings in v2
            continue
        cleaned_lines.append(line)

    while cleaned_lines and not cleaned_lines[0].strip():
        cleaned_lines.pop(0)
    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()

    return "\n".join(cleaned_lines).strip()

# -------------------------------------------------------------------
# NEW: Extract company/addressee reliably (no guessing)
# -------------------------------------------------------------------
def extract_job_header(job_description: str) -> dict:
    client = _get_client()

    prompt = f"""
Extract hiring details from this job description.

Return ONLY valid JSON:
{{
  "company": null,
  "addressee_name": null,
  "addressee_title": null
}}

Rules:
- If the company name is clearly stated, set "company".
- If a named person appears (e.g. "Contact: Jane Smith"), set addressee_name.
- If only a title is present (e.g. "Hiring Manager", "Recruitment Team"), set addressee_title.
- If not clear, return nulls.
- Do NOT guess.

JOB DESCRIPTION:
\"\"\"{job_description}\"\"\"
"""
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You extract hiring details accurately and never guess."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
    )

    txt = r.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
    try:
        data = json.loads(txt)
        if not isinstance(data, dict):
            return {"company": None, "addressee_name": None, "addressee_title": None}
        return {
            "company": data.get("company"),
            "addressee_name": data.get("addressee_name"),
            "addressee_title": data.get("addressee_title"),
        }
    except Exception:
        return {"company": None, "addressee_name": None, "addressee_title": None}

# -------------------------------------------------------------------
# 1. Tailored summary (unchanged but slightly stricter)
# -------------------------------------------------------------------
def generate_tailored_summary(cv_data: dict, job_description: str) -> str:
    client = _get_client()

    prompt = f"""
You are an expert UK CV writer.

Write a concise, achievement-focused professional summary (3–6 lines) that appears at the top of a CV.

Hard rules:
- UK spelling.
- Do NOT invent experience or qualifications.
- If the user provided an existing summary, improve it without changing facts.

Candidate data (JSON-like):
{cv_data}

Job / instructions:
{job_description}

Return ONLY the summary text. No headings, no bullets.
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You write UK-style CV summaries. You are concise and achievement-focused."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
    )

    return response.choices[0].message.content.strip()

# -------------------------------------------------------------------
# 1b. Job summary (unchanged)
# -------------------------------------------------------------------
def generate_job_summary(job_description: str) -> str:
    client = _get_client()

    prompt = f"""
You are an expert UK hiring manager.

Summarise the job description in 3–5 lines.
- Responsibilities, scope, key skills.
- UK spelling.
- Do NOT talk about the candidate.
- No fluff.

Job description:
\"\"\"{job_description}\"\"\"

Return ONLY the summary:
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You summarise job descriptions clearly and concisely using UK spelling."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
    )

    return response.choices[0].message.content.strip()

def generate_cover_letter(cv_data: dict, job_description: str, job_summary: str = "") -> str:
    client = _get_client()

    # Build a strict facts list to stop role blending
    facts = []
    for exp in (cv_data.get("experiences") or []):
        jt = exp.get("job_title") or ""
        co = exp.get("company") or ""
        if jt or co:
            facts.append(f"- {jt} at {co}".strip())

    facts_block = "\n".join(facts) if facts else "- (No experience provided)"

    header = extract_job_header(job_description or "")
    company = header.get("company") or ""
    addressee_name = header.get("addressee_name") or ""
    addressee_title = header.get("addressee_title") or ""

    prompt = f"""
You are an expert UK cover letter writer and hiring manager.

Write ONLY the body of a personalised, ATS-friendly cover letter.

Use this job summary as your primary understanding of the role:
{job_summary if job_summary.strip() else "(No job summary provided — use the job description.)"}

Hard truth rules (must follow):
- Do NOT merge roles. Each experience entry is a separate job.
- Do NOT change seniority or job titles. If someone is "Owner", keep them as Owner.
- Do NOT invent responsibilities, tools, achievements, or qualifications.
- If a detail is unclear, leave it out.

Employment facts (do not alter):
{facts_block}

Education rule (must follow):
- Education entries are located inside Candidate CV Data -> "education".
- If there is at least 1 education entry, you MUST include exactly ONE short sentence referencing
  the highest / most relevant qualification and the institution.
- Use natural UK wording such as:
  "I completed an HND in X at Y." OR "I achieved an HND in X at Y."
- Do NOT use the phrase "I hold an HND".
- If education is empty or missing, do NOT mention education at all.

Tone + structure:
- UK spelling, confident and specific, not generic.
- 4 short paragraphs maximum.
- Paragraph 1: role interest + fit (mention company if known: "{company}" if not blank)
- Paragraph 2: most relevant experience (use EXACT title + company from facts)
- Paragraph 3: second most relevant experience + transferable skills (accuracy, product info, ecommerce, teamwork)
- Paragraph 4: close with a clear, positive call-to-action

IMPORTANT formatting:
- Do NOT include any greeting lines ("Dear ...") because the template renderer adds it.
- Do NOT include address/date.
- Do NOT add the candidate name at the end.

MANDATORY EDUCATION SENTENCE (STRICT):
If Candidate CV Data includes any education entries, you MUST include exactly ONE education sentence.
It must contain:
- qualification (degree)
- institution
It must NOT contain:
- dates
- modules
- long explanation

Candidate CV Data:
{cv_data}

Job Description (use for detail only):
{job_description}

Now write ONLY the cover-letter body text:
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You write UK-style cover letters. You do not invent facts. "
                    "You keep roles separate."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.35,
    )

    raw_text = response.choices[0].message.content.strip()
    return _clean_cover_letter_body(raw_text)


# -------------------------------------------------------------------
# Wrapper used by App.py
# -------------------------------------------------------------------
def generate_cover_letter_ai(cv_data: dict, job_description: str, job_summary: str = "") -> str:
    return generate_cover_letter(cv_data, job_description, job_summary)


# -------------------------------------------------------------------
# 3. Improve bullets (slightly stricter)
# -------------------------------------------------------------------
def improve_bullets(description: str) -> str:
    client = _get_client()

    prompt = f"""
You are an expert UK CV writer.

Rewrite the following into stronger CV bullet points.

Rules:
- 3–6 bullets.
- UK spelling.
- Strong verbs, clear impact.
- Do NOT invent tools or responsibilities not implied.
- Keep facts the same.

Original:
\"\"\"{description}\"\"\"

Return ONLY improved bullet points:
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You improve CV bullet points using UK spelling. You keep them truthful but stronger."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.35,
    )

    return response.choices[0].message.content.strip()

# -------------------------------------------------------------------
# 3b. Improve skills (STRICT: skills only, no sentences)
# -------------------------------------------------------------------
def improve_skills(skills_text: str) -> str:
    client = _get_client()

    prompt = f"""
You are an expert UK CV writer.

Convert the following into a clean SKILLS list.

Hard rules (must follow):
- Output ONLY bullet points.
- Each bullet must be a SKILL keyword/phrase (1 to 3 words).
- NO full sentences.
- NO achievements/responsibilities.
- NO verbs like: developed, implemented, cultivated, led, managed, delivered, spearheaded.
- NO commas inside a bullet.
- 10 to 18 bullets max.
- UK spelling.
- Do NOT invent skills not implied by the input.

Input:
\"\"\"{skills_text}\"\"\"

Return ONLY the bullet list:
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You output only skill keywords for a CV skills section. "
                    "Never output sentences or achievements."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )

    return response.choices[0].message.content.strip()

# -------------------------------------------------------------------
# 4. Extract CV data (parser) - stricter role separation
# -------------------------------------------------------------------
def extract_cv_data(raw_text: str) -> dict:
    client = _get_client()

    schema_example = {
        "full_name": "Jane Doe",
        "title": "Software Engineer",
        "email": "jane.doe@example.com",
        "phone": "+44 7123 456789",
        "location": "London, UK",
        "summary": "Short professional summary...",
        "skills": ["Python", "SQL", "Leadership"],
        "experiences": [
            {
                "job_title": "Software Engineer",
                "company": "Example Ltd",
                "location": "London, UK",
                "start_date": "Jan 2020",
                "end_date": "Present",
                "description": "• Bullet 1\n• Bullet 2",
            }
        ],
        "education": [
            {
                "degree": "BSc Computer Science",
                "institution": "Example University",
                "location": "Manchester, UK",
                "start_date": "Sep 2016",
                "end_date": "Jun 2019",
            }
        ],
    }

    prompt = f"""
You are a CV parser. Extract structured data from the CV text below.

CRITICAL rules:
- Keep each job role separate. Do NOT merge roles from different employers.
- experiences must be a list; each item must be exactly one job.
- job_title and company must match the CV wording as closely as possible.
- descriptions must only contain content belonging to that job.

Return ONLY valid JSON (no markdown, no backticks, no explanation) matching this schema:
{json.dumps(schema_example, indent=2)}

If a field is missing, use null or an empty list as appropriate.

CV TEXT:
\"\"\"{raw_text}\"\"\""""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You parse CVs into structured JSON suitable for filling forms. You never merge jobs."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
    )

    content = response.choices[0].message.content.strip()
    content = content.replace("```json", "").replace("```", "").strip()

    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}
