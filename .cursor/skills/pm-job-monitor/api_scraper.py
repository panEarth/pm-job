"""API fallbacky pro portály blokované v browseru."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = "Mozilla/5.0 (compatible; PMJobMonitor/1.0)"


def _post_json(url: str, payload: dict, timeout: int = 30) -> tuple[dict | None, str | None]:
    try:
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            url,
            data=data,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace")), None
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        return None, f"HTTP {exc.code}: {body}"
    except URLError as exc:
        return None, str(exc.reason)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _get_json(url: str, timeout: int = 30) -> tuple[dict | None, str | None]:
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace")), None
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        return None, f"HTTP {exc.code}: {body}"
    except URLError as exc:
        return None, str(exc.reason)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def fetch_jooble_api(
    portal: str,
    keywords: str = "product manager",
    location: str = "Praha",
    max_pages: int = 2,
) -> tuple[list[dict], str | None]:
    api_key = os.environ.get("JOOBLE_API_KEY", "").strip()
    if not api_key:
        return [], "Chybí JOOBLE_API_KEY (registrace: https://jooble.org/api/about)"

    jobs: list[dict] = []
    for page in range(1, max_pages + 1):
        data, err = _post_json(
            f"https://jooble.org/api/{api_key}",
            {
                "keywords": keywords,
                "location": location,
                "page": str(page),
                "companysearch": "false",
            },
        )
        if err:
            return jobs, err if not jobs else None
        for item in data.get("jobs", []):
            jobs.append({
                "title": item.get("title", "").strip(),
                "company": item.get("company", "").strip(),
                "location": item.get("location", "").strip(),
                "url": item.get("link", "").strip(),
                "portal": portal,
            })
        if not data.get("jobs"):
            break
    return jobs, None


def fetch_adzuna_api(
    portal: str,
    what: str = "product manager",
    where: str = "Praha",
    max_pages: int = 2,
) -> tuple[list[dict], str | None]:
    app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not app_id or not app_key:
        return [], "Chybí ADZUNA_APP_ID/ADZUNA_APP_KEY (registrace: https://developer.adzuna.com/signup)"

    jobs: list[dict] = []
    for page in range(1, max_pages + 1):
        url = (
            f"https://api.adzuna.com/v1/api/jobs/cz/search/{page}"
            f"?app_id={app_id}&app_key={app_key}"
            f"&results_per_page=50&what={_quote(what)}&where={_quote(where)}"
            f"&content-type=application/json"
        )
        data, err = _get_json(url)
        if err:
            return jobs, err if not jobs else None
        for item in data.get("results", []):
            jobs.append({
                "title": item.get("title", "").strip(),
                "company": item.get("company", {}).get("display_name", "").strip(),
                "location": item.get("location", {}).get("display_name", "").strip(),
                "url": item.get("redirect_url", "").strip(),
                "portal": portal,
            })
        if page >= data.get("pages", page):
            break
    return jobs, None


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value)
