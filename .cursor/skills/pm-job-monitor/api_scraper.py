"""API fallbacky pro portály blokované v browseru."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

USER_AGENT = "Mozilla/5.0 (compatible; PMJobMonitor/1.0)"
BASE_DIR = Path(__file__).resolve().parent
LOCAL_KEYS_FILE = BASE_DIR / "api-keys.local.json"


def _load_local_keys() -> dict:
    if not LOCAL_KEYS_FILE.exists():
        return {}
    try:
        with LOCAL_KEYS_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _credential(env_name: str, *local_path: str) -> str:
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    data = _load_local_keys()
    node = data
    for key in local_path:
        if not isinstance(node, dict):
            return ""
        node = node.get(key, "")
    return str(node).strip() if node else ""


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
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
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
    location: str = "Czech Republic",
    max_pages: int = 2,
) -> tuple[list[dict], str | None]:
    api_key = _credential("JOOBLE_API_KEY", "jooble", "api_key")
    if not api_key:
        return [], "Chybí JOOBLE_API_KEY (registrace: https://jooble.org/api/about)"

    czech_markers = (
        "czech", "prague", "praha", "brno", "česk", "cesk", "czechia", "czech republic",
        "cz.jooble",
    )
    us_markers = (
        ", tx", ", ca", ", ny", ", fl", ", ne", ", nc", ", usa", "united states",
        ", il", ", oh", ", pa", ", ga", ", va", ", wa", ", co", ", az",
    )

    def is_czech_job(item: dict) -> bool:
        text = " ".join([
            item.get("title", ""),
            item.get("location", ""),
            item.get("snippet", "")[:300],
            item.get("link", ""),
        ]).lower()
        if any(marker in text for marker in us_markers):
            return False
        return any(marker in text for marker in czech_markers)

    def query_jooble(query_location: str) -> tuple[list[dict], bool]:
        batch: list[dict] = []
        auth_ok = False
        for page in range(1, max_pages + 1):
            data, err = _post_json(
                f"https://jooble.org/api/{api_key}",
                {
                    "keywords": keywords,
                    "location": query_location,
                    "page": str(page),
                    "companysearch": "false",
                    "ResultOnPage": "50",
                },
            )
            if err:
                if "403" in err:
                    return batch, False
                continue
            auth_ok = True
            for item in data.get("jobs", []):
                if query_location.lower() in {"czech republic", "česká republika", "czechia"}:
                    if not is_czech_job(item) and item.get("location", "").strip():
                        continue
                elif query_location.lower() == "europe" and not is_czech_job(item):
                    continue
                batch.append({
                    "title": item.get("title", "").strip(),
                    "company": item.get("company", "").strip(),
                    "location": item.get("location", "").strip(),
                    "url": item.get("link", "").strip(),
                    "portal": portal,
                })
            if not data.get("jobs") or page >= (data.get("totalCount", 0) + 49) // 50:
                break
        return batch, auth_ok

    jobs, auth_ok = query_jooble(location)
    if not jobs and location.lower() != "europe":
        extra, extra_auth = query_jooble("Europe")
        auth_ok = auth_ok or extra_auth
        seen = {j["url"] for j in jobs}
        for job in extra:
            if job["url"] not in seen:
                jobs.append(job)
                seen.add(job["url"])

    if not auth_ok:
        return [], "Jooble API: neplatný API klíč nebo nedostupné"
    if not jobs:
        return [], "Jooble API funguje, ale nevrací CZ PM pozice (index bez českého trhu)"
    return jobs, None


def fetch_adzuna_api(
    portal: str,
    what: str = "product manager",
    where: str = "Praha",
    max_pages: int = 2,
) -> tuple[list[dict], str | None]:
    app_id = _credential("ADZUNA_APP_ID", "adzuna", "app_id")
    app_key = _credential("ADZUNA_APP_KEY", "adzuna", "app_key")
    if not app_id:
        return [], "Chybí ADZUNA_APP_ID — najdeš ho na https://developer.adzuna.com/home"
    if not app_key:
        return [], "Chybí ADZUNA_APP_KEY (registrace: https://developer.adzuna.com/signup)"

    # Adzuna nepodporuje ISO kód cz — hledáme v sousedních zemích a filtrujeme CZ lokace
    country_queries = [
        ("pl", what, where),
        ("de", what, where),
        ("at", what, where),
    ]
    czech_markers = (
        "czech", "prague", "praha", "brno", "česk", "cesk", "česko", "czechia", "czech republic",
    )

    jobs: list[dict] = []
    seen_urls: set[str] = set()
    auth_ok = False

    for country, query, location in country_queries:
        for page in range(1, max_pages + 1):
            params = (
                f"app_id={app_id}&app_key={app_key}"
                f"&results_per_page=50&what={quote(query)}"
                f"&content-type=application/json"
            )
            if location:
                params += f"&where={quote(location)}"
            url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?{params}"
            data, err = _get_json(url)
            if err:
                if "AUTH_FAIL" in err or "401" in err:
                    return [], "Adzuna API: neplatné credentials (app_id/app_key)"
                continue
            auth_ok = True
            for item in data.get("results", []):
                title = item.get("title", "").strip()
                company = item.get("company", {}).get("display_name", "").strip()
                loc = item.get("location", {}).get("display_name", "").strip()
                job_url = item.get("redirect_url", "").strip()
                text = f"{title} {loc} {item.get('description', '')[:300]}".lower()
                if not any(marker in text for marker in czech_markers):
                    continue
                if job_url in seen_urls:
                    continue
                seen_urls.add(job_url)
                jobs.append({
                    "title": title,
                    "company": company,
                    "location": loc,
                    "url": job_url,
                    "portal": portal,
                })
            if page >= data.get("pages", page):
                break

    if not auth_ok:
        return [], "Adzuna API nedostupné — zkontroluj credentials"
    if not jobs:
        return [], "Adzuna API funguje, ale nepodporuje Česko — nenalezeny CZ PM pozice"
    return jobs, None


def fetch_himalayas_api(
    portal: str,
    query: str = "product manager",
    country: str = "Czechia",
    max_pages: int = 2,
) -> tuple[list[dict], str | None]:
    """Veřejné Himalayas API — https://himalayas.app/docs/remote-jobs-api"""
    jobs: list[dict] = []
    for page in range(1, max_pages + 1):
        url = (
            f"https://himalayas.app/jobs/api/search"
            f"?q={quote(query)}&country={quote(country)}&page={page}"
        )
        data, err = _get_json(url, timeout=60)
        if err:
            return jobs, err if not jobs else None

        batch = data.get("jobs", [])
        for item in batch:
            locations = item.get("locationRestrictions") or []
            location = ", ".join(locations) if locations else "Remote"
            job_url = (item.get("applicationLink") or item.get("guid") or "").strip()
            jobs.append({
                "title": item.get("title", "").strip(),
                "company": item.get("companyName", "").strip(),
                "location": location,
                "url": job_url,
                "portal": portal,
            })

        total = data.get("totalCount", 0)
        if not batch or page * data.get("limit", 20) >= total:
            break
    return jobs, None
