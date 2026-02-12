# adzuna_client.py
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests


ADZUNA_BASE_URL = "https://api.adzuna.com/v1/api/jobs"
DEFAULT_COUNTRY = "gb"  # UK marketplace


class AdzunaConfigError(RuntimeError):
    pass


class AdzunaAPIError(RuntimeError):
    pass


def _get_keys() -> tuple[str, str]:
    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise AdzunaConfigError(
            "Missing Adzuna API keys. Set ADZUNA_APP_ID and ADZUNA_APP_KEY in Railway variables."
        )
    return app_id, app_key


def search_jobs(query: str, location: str, results: int = 10) -> List[Dict[str, Any]]:
    """
    Search Adzuna job listings.

    Returns a list[dict] where each dict includes:
      title, company, location, description, url, salary_min, salary_max, created
    """
    app_id, app_key = _get_keys()

    query = (query or "").strip()
    location = (location or "").strip()
    if not query:
        # Keep API calls clean; caller can decide how to message the user
        return []

    # Adzuna search endpoint:
    # GET /v1/api/jobs/{country}/search/{page}
    url = f"{ADZUNA_BASE_URL}/{DEFAULT_COUNTRY}/search/1"

    params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": max(1, min(int(results), 50)),  # Adzuna supports more, but we cap for UI
        "what": query,
        "where": location if location else None,
        "content-type": "application/json",
    }
    # Remove None params (requests would still send "where=None")
    params = {k: v for k, v in params.items() if v is not None}

    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            raise AdzunaAPIError(f"Adzuna returned status {resp.status_code}")
        data = resp.json()
    except requests.RequestException as e:
        raise AdzunaAPIError("Network error calling Adzuna") from e
    except ValueError as e:
        raise AdzunaAPIError("Invalid JSON returned by Adzuna") from e

    results_list = data.get("results") or []
    jobs: List[Dict[str, Any]] = []

    for item in results_list:
        # Safe extraction with fallbacks
        title = item.get("title") or ""
        company = (item.get("company") or {}).get("display_name") or ""
        loc = (item.get("location") or {}).get("display_name") or ""
        description = item.get("description") or ""
        url_out = item.get("redirect_url") or item.get("adref") or ""
        salary_min = item.get("salary_min")
        salary_max = item.get("salary_max")
        created = item.get("created") or ""  # often ISO timestamp

        jobs.append(
            {
                "title": title,
                "company": company,
                "location": loc,
                "description": description,
                "url": url_out,
                "salary_min": salary_min,
                "salary_max": salary_max,
                "created": created,
            }
        )

    return jobs
