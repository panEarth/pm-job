#!/usr/bin/env python3
"""PM Job Monitor — denní sken portálů."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import html as html_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[2]  # pm-job/.cursor/skills/pm-job-monitor → pm-job
PORTALS_FILE = BASE_DIR / "portals.json"
FILTERS_FILE = BASE_DIR / "filters.json"
STATE_FILE = BASE_DIR / "state" / "seen-jobs.json"
WEB_JOBS_FILE = REPO_ROOT / "docs" / "jobs.json"

USER_AGENT = "Mozilla/5.0 (compatible; PMJobMonitor/1.0)"
REQUEST_DELAY = 1.0


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def fetch(url: str, timeout: int = 30) -> tuple[str | None, str | None]:
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace"), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
             if not k.startswith("utm_") and k not in {"ref", "source", "rps", "searchId"}]
    flat_query = []
    for k, vals in query:
        for v in vals:
            flat_query.append((k, v))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", urlencode(flat_query), ""))


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def normalize_company(company: str) -> str:
    return re.sub(r"\s+", " ", (company or "").strip().lower())


def nofluff_base_slug(url: str) -> str:
    """Strip region/city suffixes NoFluffJobs appends for multi-location posts."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    # Keep chopping known location/region tails until stable
    region_tail = re.compile(
        r"-(?:"
        r"remote|fully-remote|hybrid|"
        r"warszawa|warsaw|krakow|krakow|kraków|wroclaw|wrocław|"
        r"poznan|poznań|gdansk|gdańsk|katowice|lodz|łódź|lublin|"
        r"prague|praha|brno|ostrava|"
        r"poland|poland-\w+|czechia|czech-republic|"
        r"lower-silesian|kuyavian-pomeranian|lubusz|lesser-poland|opole|"
        r"subcarpathian|podlaskie|pomeranian|silesian|holy-cross|"
        r"warmian-masurian|greater-poland|west-pomeranian|masovian|"
        r"lodzkie|swietokrzyskie|podkarpackie|malopolskie|dolnoslaskie|"
        r"zachodniopomorskie|warminsko-mazurskie|kujawsko-pomorskie|"
        r"pl|cz|sk|hu|de|at|nl"
        r")(?:-\d+)?$",
        re.I,
    )
    prev = None
    while prev != slug:
        prev = slug
        slug = region_tail.sub("", slug)
    return slug


def prefer_job_url(current: str, candidate: str) -> str:
    """Prefer a canonical remote/generic NoFluff URL over a region-specific one."""
    if not current:
        return candidate
    if not candidate:
        return current
    if "nofluffjobs.com/job/" not in current and "nofluffjobs.com/job/" not in candidate:
        return current

    def score(url: str) -> tuple[int, int]:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        base = nofluff_base_slug(url)
        # Higher is better: exact base > remote suffix > other region variants
        if slug == base:
            tier = 3
        elif slug.endswith("-remote") or slug.endswith("-fully-remote"):
            tier = 2
        else:
            tier = 1
        # Prefer shorter slug within the same tier
        return (tier, -len(slug))

    return candidate if score(candidate) > score(current) else current


def dedupe_key(job: dict) -> str:
    title = normalize_title(job.get("title", ""))
    company = normalize_company(job.get("company", ""))
    url = job.get("url", "")
    portal = (job.get("portal") or "").lower()

    # NoFluffJobs publishes one URL per region — treat as one job.
    if "nofluffjobs.com/job/" in url or "nofluff" in portal:
        if company and title:
            return f"nofluff|{company}|{title}"
        return f"nofluff|{nofluff_base_slug(url)}|{title}"

    # Same role at same company across portals (Jobs.cz + StartupJobs, …)
    if company and title:
        return f"role|{company}|{title}"

    norm = normalize_url(url)
    if norm:
        return f"url|{norm}"
    return f"text|{company}|{title}"


def job_id(url: str, title: str, company: str, portal: str = "") -> str:
    """Stable ID — for NoFluff / known company+title ignore region URL variants."""
    key_job = {"url": url, "title": title, "company": company, "portal": portal}
    key = dedupe_key(key_job)
    if key.startswith(("nofluff|", "role|")):
        return hashlib.sha256(key.encode()).hexdigest()[:16]
    norm = normalize_url(url)
    if norm and norm != "https://www.jobs.cz":
        return hashlib.sha256(norm.encode()).hexdigest()[:16]
    fallback = f"{normalize_company(company)}|{normalize_title(title)}"
    return hashlib.sha256(fallback.encode()).hexdigest()[:16]


def merge_location(a: str, b: str) -> str:
    parts = []
    for raw in (a or "", b or ""):
        for p in raw.split(","):
            p = p.strip()
            if p:
                parts.append(p)
    return ", ".join(sorted(dict.fromkeys(parts), key=str.casefold))


def collapse_duplicate_jobs(jobs: list[dict]) -> list[dict]:
    """Merge existing state duplicates (NoFluff region variants + cross-portal)."""
    merged: dict[str, dict] = {}
    for job in jobs:
        key = dedupe_key(job)
        if key not in merged:
            merged[key] = dict(job)
            merged[key]["id"] = job_id(
                job.get("url", ""),
                job.get("title", ""),
                job.get("company", ""),
                job.get("portal", ""),
            )
            continue
        rec = merged[key]
        rec["url"] = prefer_job_url(rec.get("url", ""), job.get("url", ""))
        rec["location"] = merge_location(rec.get("location", ""), job.get("location", ""))
        # Keep earliest firstSeen, latest lastSeen
        if (job.get("firstSeen") or "9999") < (rec.get("firstSeen") or "9999"):
            rec["firstSeen"] = job.get("firstSeen")
        if (job.get("lastSeen") or "") > (rec.get("lastSeen") or ""):
            rec["lastSeen"] = job.get("lastSeen")
        if not rec.get("company") and job.get("company"):
            rec["company"] = job["company"]
        # Prefer non-NoFluff portal label only if we somehow merged; keep first otherwise
        if "nofluff" in (rec.get("portal") or "").lower() and "nofluff" not in (job.get("portal") or "").lower():
            rec["portal"] = job.get("portal", rec.get("portal"))
    return list(merged.values())


def filter_jobs(jobs: list[dict], filters: dict) -> list[dict]:
    filtered = []
    seen = set()
    for job in jobs:
        title = job.get("title", "").strip()
        if not title:
            continue
        if not matches_include(title, filters["includeKeywords"]):
            continue
        if matches_exclude(title, filters["excludeKeywords"]):
            continue
        if not location_ok(job.get("location", ""), title, filters):
            continue
        key = dedupe_key(job)
        if key in seen:
            # Prefer remote/canonical URL if we already kept a region variant
            for i, kept in enumerate(filtered):
                if dedupe_key(kept) == key:
                    filtered[i] = {
                        **kept,
                        "url": prefer_job_url(kept.get("url", ""), job.get("url", "")),
                        "location": merge_location(kept.get("location", ""), job.get("location", "")),
                    }
                    break
            continue
        seen.add(key)
        job["url"] = normalize_url(job.get("url", "")) or job.get("url", "")
        filtered.append(job)
    return filtered


def update_state(state: dict, found_jobs: list[dict], now: datetime) -> tuple[list[dict], list[dict]]:
    today = now.date().isoformat()
    # Collapse historical NoFluff/cross-portal duplicates first
    existing_list = collapse_duplicate_jobs(state.get("jobs", []))
    existing = {j["id"]: j for j in existing_list}
    # Also index by dedupe key for matching renamed IDs
    by_key = {dedupe_key(j): j for j in existing_list}
    new_jobs = []
    updated_jobs = []

    for job in found_jobs:
        jid = job_id(job.get("url", ""), job["title"], job.get("company", ""), job.get("portal", ""))
        key = dedupe_key(job)
        rec = existing.get(jid) or by_key.get(key)
        if rec:
            old_title = rec.get("title", "")
            rec["id"] = jid
            rec["lastSeen"] = today
            rec["title"] = job["title"]
            rec["company"] = job.get("company", "") or rec.get("company", "")
            rec["location"] = merge_location(rec.get("location", ""), job.get("location", ""))
            rec["url"] = prefer_job_url(rec.get("url", ""), job.get("url", ""))
            rec["portal"] = job.get("portal", rec.get("portal", ""))
            existing[jid] = rec
            by_key[key] = rec
            if old_title and old_title != job["title"]:
                updated_jobs.append({**job, "id": jid, "oldTitle": old_title})
        else:
            rec = {
                "id": jid,
                "title": job["title"],
                "company": job.get("company", ""),
                "location": job.get("location", ""),
                "url": job.get("url", ""),
                "portal": job.get("portal", ""),
                "firstSeen": today,
                "lastSeen": today,
            }
            existing[jid] = rec
            by_key[key] = rec
            new_jobs.append(rec)

    cutoff = (now.date() - timedelta(days=90)).isoformat()
    jobs_list = collapse_duplicate_jobs(list(existing.values()))
    jobs_list = [j for j in jobs_list if j.get("lastSeen", "1970-01-01") >= cutoff]
    jobs_list.sort(key=lambda x: x.get("lastSeen", ""), reverse=True)
    jobs_list = jobs_list[:2000]

    state["jobs"] = jobs_list
    state["lastRun"] = now.isoformat()
    return new_jobs, updated_jobs


def matches_include(title: str, include_keywords: list[str]) -> bool:
    tl = title.lower()
    return any(k.lower() in tl for k in include_keywords)


def matches_exclude(title: str, exclude_keywords: list[str]) -> bool:
    tl = title.lower()
    return any(k.lower() in tl for k in exclude_keywords)


REJECT_LOCATIONS = [
    "santa clara", "san francisco", "california", "usa", "united states",
    "bellevue", "raleigh", "texas", "seattle", "new york", "chicago",
    "mountain view", "palo alto", "silicon valley",
]

# Pure onsite outside Prague — reject unless hybrid/remote is also present
ONSITE_OUTSIDE_PRAGUE = [
    "brno", "ostrava", "plzen", "plzeň", "olomouc", "liberec", "hradec",
    "ceske budejovice", "české budějovice", "pardubice", "zlin", "zlín",
    "karlovy vary", "usti nad labem", "ústí nad labem", "jihlava",
]

EU_LOCATION_HINTS = [
    "poland", "warszawa", "warsaw", "kraków", "krakow", "wrocław", "wroclaw",
    "poznań", "poznan", "gdańsk", "gdansk", "sopot", "bratislava", "slovakia",
    "germany", "berlin", "munich", "austria", "vienna", "hungary", "budapest",
]

PRAGUE_HINTS = ["praha", "prague", "prague 1", "prague 2", "praha 1", "praha 2"]
REMOTE_HINTS = [
    "remote", "full remote", "fully remote", "na dalku", "na dálku",
    "work from anywhere", "wfa", "home office", "plne na dalku", "plně na dálku",
]
HYBRID_HINTS = ["hybrid", "flexibilni", "flexibilní"]


def _norm_loc_text(location: str, title: str = "") -> str:
    return f"{location or ''} {title or ''}".lower()


def is_remote_like(text: str) -> bool:
    return any(k in text for k in REMOTE_HINTS)


def is_hybrid_like(text: str) -> bool:
    return any(k in text for k in HYBRID_HINTS)


def is_prague_like(text: str) -> bool:
    return any(k in text for k in PRAGUE_HINTS)


def is_onsite_outside_prague(text: str) -> bool:
    if is_prague_like(text) or is_remote_like(text) or is_hybrid_like(text):
        return False
    return any(k in text for k in ONSITE_OUTSIDE_PRAGUE)


def location_ok(location: str, title: str, filters: dict) -> bool:
    """Accept: Praha onsite, hybrid anywhere CZ-ish, full remote CZ/EU. Reject Brno-only onsite."""
    if not filters.get("locationRequired", True):
        return True
    loc = (location or "").strip()
    text = _norm_loc_text(loc, title)
    reject = [k.lower() for k in filters.get("rejectLocations", REJECT_LOCATIONS)]
    if loc and any(k in text for k in reject):
        return False

    # Explicit onsite outside Prague (e.g. Brno) without hybrid/remote
    if is_onsite_outside_prague(text):
        return False

    if is_prague_like(text) or is_hybrid_like(text) or is_remote_like(text):
        return True

    remote_eu = [k.lower() for k in filters.get("locations", {}).get("remoteAlsoAccept", [])]
    if any(k in text for k in remote_eu):
        return True
    if any(k in text for k in EU_LOCATION_HINTS) and is_remote_like(text):
        return True
    # EU remote often says only city+country without "remote" word but portal marked remote earlier
    if any(k in text for k in EU_LOCATION_HINTS) and "remote" in text:
        return True

    # Jobs.cz cards sometimes lack location — keep until detail enrichment fills it
    if not loc:
        return True
    return False


def nofluff_location_ok(posting: dict, filters: dict) -> bool:
    loc = posting.get("location", {})
    places = loc.get("places", [])
    parts = []
    for pl in places:
        parts.append(pl.get("city", ""))
        parts.append(pl.get("country", {}).get("name", ""))
    if posting.get("fullyRemote"):
        parts.append("remote")
    text = " ".join(parts).lower()

    if is_onsite_outside_prague(text) and not posting.get("fullyRemote"):
        return False
    if is_prague_like(text) or is_hybrid_like(text) or posting.get("fullyRemote"):
        return True
    remote_eu = [k.lower() for k in filters.get("locations", {}).get("remoteAlsoAccept", [])]
    if posting.get("fullyRemote") and (any(k in text for k in remote_eu) or not places):
        return True
    # Czech region alone is not enough (would include Brno onsite)
    if "cz" in (posting.get("regions") or []) and is_prague_like(text):
        return True
    if any(k in text for k in remote_eu) and posting.get("fullyRemote"):
        return True
    return False

def scrape_tribee_browser(portal: dict) -> tuple[list[dict], str | None]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], "Playwright není nainstalován — vyžaduje browser"

    name = portal["name"]
    query = portal.get("searchQuery", "product manager")
    jobs: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            for page_num in (1, 2):
                base = portal.get("url", "https://www.tribee.cz/cs/prace")
                url = f"{base}?q={query.replace(' ', '+')}"
                if page_num > 1:
                    url += f"&page={page_num}"
                page.goto(url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(1500)
                items = page.evaluate("""
                    () => Array.from(document.querySelectorAll('a[href*="/spolecnost/"][href*="/prace/"]'))
                        .map(a => {
                            const parts = (a.innerText || '').trim().split('\\n').map(s => s.trim()).filter(Boolean);
                            return {
                                title: parts[0] || '',
                                company: parts[1] || '',
                                location: parts[2] || '',
                                url: a.href
                            };
                        })
                        .filter(j => j.title && j.url)
                """)
                for item in items:
                    item["portal"] = name
                    jobs.append(item)
            browser.close()
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)
    return jobs, None


def parse_jobs_cz(html_text: str, portal: str) -> list[dict]:
    articles = re.findall(r"<article[\s\S]*?</article>", html_text)
    jobs = []
    for article in articles:
        title_m = re.search(r'data-test-ad-title="([^"]+)"', article)
        if not title_m:
            continue
        title = html_lib.unescape(title_m.group(1))

        company = ""
        # 1) logo alt (when logo is present)
        company_m = re.search(
            r'class="[^"]*CompanyLogo[^"]*"[\s\S]*?<img[^>]*alt="([^"]+)"',
            article,
            re.I,
        )
        if company_m:
            company = html_lib.unescape(company_m.group(1).strip())
        # 2) footer: first item is company (<span translate="no">…</span>)
        if not company:
            footer_m = re.search(
                r'SearchResultCard__footerItem"[\s\S]*?<span[^>]*>([^<]+)</span>',
                article,
            )
            if footer_m:
                company = html_lib.unescape(footer_m.group(1).strip())
        # 3) fallback: title "… | Firma"
        if not company and "|" in title:
            company = title.rsplit("|", 1)[-1].strip()

        loc_m = re.search(
            r'data-test="(?:location|serp-locality)"[^>]*>\s*([^<]+)<',
            article,
        )
        if not loc_m:
            loc_m = re.search(
                r'data-test="serp-locality"[\s\S]*?</svg>\s*([^<]+)<',
                article,
            )
        location = html_lib.unescape(loc_m.group(1).strip()) if loc_m else ""

        link_m = re.search(r'href="(https://www\.jobs\.cz/rpd/\d+/)', article)
        if not link_m:
            continue
        url = link_m.group(1)
        jobs.append({
            "title": title,
            "company": company,
            "location": location,
            "url": url,
            "portal": portal,
        })
    return jobs


def parse_startupjobs_offer(html_text: str, url: str, portal: str) -> dict | None:
    title_m = re.search(r"<title>([^<|]+)", html_text)
    if not title_m:
        return None
    title = html_lib.unescape(title_m.group(1).strip())
    if title.endswith("| StartupJobs.cz"):
        title = title[: -len("| StartupJobs.cz")].strip()

    company = ""
    for pat in [
        r'"hiringOrganization"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]+)"',
        r'"company"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]+)"',
        r'"companyName"\s*:\s*"([^"]+)"',
        r'itemprop="hiringOrganization"[^>]*>[\s\S]*?itemprop="name"[^>]*>([^<]+)<',
    ]:
        m = re.search(pat, html_text, re.I | re.S)
        if m:
            company = html_lib.unescape(m.group(1).strip())
            break

    location = ""
    for pat in [
        r'"addressLocality"\s*:\s*"([^"]+)"',
        r'Lokalita[\s\S]{0,200}?>([^<]{2,80})<',
        r'(?:Hybrid|Remote|Praha|Brno)[^<]{0,40}',
    ]:
        m = re.search(pat, html_text, re.I)
        if m:
            location = html_lib.unescape(m.group(1 if m.lastindex else 0).strip())
            break

    return {
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        "portal": portal,
    }


def startupjobs_pm_urls() -> list[str]:
    content, err = fetch("https://www.startupjobs.cz/sitemap/offers.xml")
    if err or not content:
        return []
    urls = re.findall(r"<loc>(https://www\.startupjobs\.cz/nabidka/[^<]+)</loc>", content)
    keys = [
        "product-manager", "product-owner", "produktov", "head-of-product",
        "product-lead", "product-director", "chief-product", "vp-product", "/cpo",
        "ai-product", "product-ai", "technical-product", "growth-product",
        "platform-product", "group-product", "principal-product", "staff-product",
    ]
    return [u for u in urls if any(k in u.lower() for k in keys)]


def parse_nofluffjobs(posting: dict, portal: str) -> dict:
    loc = posting.get("location", {})
    places = loc.get("places", [])
    parts: list[str] = []
    for pl in places:
        city = pl.get("city", "")
        country = pl.get("country", {}).get("name", "")
        if city:
            parts.append(city)
        if country:
            parts.append(country)
    if posting.get("fullyRemote"):
        parts.append("Remote")
    # Stable order so the same job doesn't look different across region variants
    location = ", ".join(sorted(dict.fromkeys(p for p in parts if p), key=str.casefold))
    return {
        "title": posting.get("title", "").strip(),
        "company": posting.get("name", "").strip(),
        "location": location,
        "url": f"https://nofluffjobs.com/job/{posting.get('url', '')}",
        "portal": portal,
    }


def parse_jobs_cz_detail_location(html_text: str) -> str:
    """Extract workplace location from a Jobs.cz detail page."""
    patterns = [
        r'data-test="jd-info-location"[^>]*>([^<]+)<',
        r'"addressLocality"\s*:\s*"([^"]+)"',
        r'itemprop="addressLocality"[^>]*(?:content="([^"]+)"|>([^<]+)<)',
    ]
    for pat in patterns:
        m = re.search(pat, html_text, re.I)
        if not m:
            continue
        loc = next((g for g in m.groups() if g), "")
        loc = html_lib.unescape(loc.strip())
        if loc:
            return loc
    # Workplace mode badges
    modes = []
    for label, keys in (
        ("Hybrid", HYBRID_HINTS),
        ("Remote", REMOTE_HINTS),
    ):
        if any(k in html_text.lower() for k in keys):
            modes.append(label)
    return ", ".join(modes)


def enrich_jobs_cz_locations(jobs: list[dict]) -> list[dict]:
    """Fetch detail pages when list-card location is missing or ambiguous."""
    enriched = []
    for job in jobs:
        loc = (job.get("location") or "").strip()
        needs_detail = (not loc) or is_onsite_outside_prague(_norm_loc_text(loc))
        if needs_detail and job.get("url"):
            content, err = fetch(job["url"])
            time.sleep(REQUEST_DELAY)
            if not err and content:
                detail_loc = parse_jobs_cz_detail_location(content)
                if detail_loc:
                    job = {**job, "location": detail_loc}
        enriched.append(job)
    return enriched


def scan_portal(portal: dict, filters: dict) -> tuple[list[dict], str | None]:
    name = portal["name"]
    ptype = portal["type"]

    if name == "Jobs.cz":
        all_jobs = []
        for page in (1, 2):
            url = portal["searchUrl"] if page == 1 else portal["searchUrl"] + "&page=2"
            content, err = fetch(url)
            time.sleep(REQUEST_DELAY)
            if err:
                return [], err
            all_jobs.extend(parse_jobs_cz(content or "", name))
        all_jobs = enrich_jobs_cz_locations(all_jobs)
        return all_jobs, None

    if name == "StartupJobs.cz":
        jobs = []
        urls = startupjobs_pm_urls()[:40]
        for url in urls:
            content, err = fetch(url)
            time.sleep(REQUEST_DELAY)
            if err or not content:
                continue
            job = parse_startupjobs_offer(content, url, name)
            if job:
                jobs.append(job)
        return jobs, None if jobs else "Nepodařilo se načíst nabídky ze sitemap"

    if name == "NoFluffJobs CZ":
        api_url = "https://nofluffjobs.com/api/posting?limit=1000&criteria=category%3DproductManagement"
        content, err = fetch(api_url)
        time.sleep(REQUEST_DELAY)
        if err:
            return [], err
        try:
            data = json.loads(content or "{}")
        except json.JSONDecodeError:
            return [], "Neplatná JSON odpověď API"
        jobs = []
        for posting in data.get("postings", []):
            if posting.get("category") != "productManagement":
                continue
            title = posting.get("title", "")
            if not matches_include(title, filters["includeKeywords"]):
                continue
            if matches_exclude(title, filters["excludeKeywords"]):
                continue
            if not nofluff_location_ok(posting, filters):
                continue
            jobs.append(parse_nofluffjobs(posting, name))
        return jobs, None

    if name == "Tribee":
        return scrape_tribee_browser(portal)

    if name in {"Indeed CZ", "Jooble CZ"}:
        target = portal.get("searchUrl") or portal.get("url", "")
        content, err = fetch(target)
        time.sleep(REQUEST_DELAY)
        if err:
            return [], err
        if content and ("Just a moment" in content or "challenge-platform" in content):
            return [], "Cloudflare ochrana — vyžaduje browser"
        if content and len(content) < 5000:
            return [], "Prázdná nebo blokovaná odpověď"
        return [], "SPA bez veřejného API — vyžaduje browser"

    if ptype == "search_url":
        content, err = fetch(portal["searchUrl"])
        time.sleep(REQUEST_DELAY)
        if err:
            return [], err
        return [], "Nepodporovaný portál bez parseru"

    return [], "Neznámý typ portálu"


def build_report(
    new_jobs: list[dict],
    updated_jobs: list[dict],
    portal_count: int,
    failures: list[tuple[str, str]],
    now: datetime,
) -> str:
    date_str = now.strftime("%d.%m.%Y")
    time_str = now.strftime("%H:%M")

    lines = []
    if failures:
        lines.append(f"⚠️ *PM Job Monitor — varování — {date_str}*")
        lines.append("")
        for portal, reason in failures:
            lines.append(f"• *{portal}*: {reason}")
        lines.append("")

    report_items = new_jobs + [
        {**u, "title": f"{u['title']} _(aktualizováno z: {u.get('oldTitle', '')})_"}
        for u in updated_jobs
    ]

    if report_items:
        header = f"🔍 *PM Job Monitor — {date_str}*"
        if not failures:
            lines = [header]
        else:
            lines.insert(0, header)
        count = len(report_items)
        lines.append("")
        lines.append(f"Nalezeno *{count} nových* pozic:")
        lines.append("")
        for i, job in enumerate(report_items, 1):
            lines.append(f"*{i}. {job['title']}*")
            company = job.get("company") or "Neznámá firma"
            location = job.get("location") or "Neuvedeno"
            lines.append(f"🏢 {company} · 📍 {location}")
            lines.append(f"🔗 {job.get('url', '')}")
            lines.append(f"📌 Zdroj: {job.get('portal', '')}")
            if i < len(report_items):
                lines.append("")
                lines.append("---")
                lines.append("")
        lines.append("")
        lines.append(f"_Celkem monitorováno: {portal_count} portálů · Poslední běh: {time_str}_")
    elif not failures:
        lines = [
            f"✅ *PM Job Monitor — {date_str}*",
            "",
            f"Žádné nové PM pozice. Monitorováno {portal_count} portálů.",
        ]
    else:
        lines.append(f"_Celkem monitorováno: {portal_count} portálů · Poslední běh: {time_str}_")

    return "\n".join(lines)


def export_web(state: dict) -> None:
    """Export jobs for the public docs/ overview page."""
    jobs = sorted(
        state.get("jobs", []),
        key=lambda j: (j.get("firstSeen") or "", j.get("lastSeen") or "", j.get("title") or ""),
        reverse=True,
    )
    payload = {
        "lastRun": state.get("lastRun"),
        "generatedAt": state.get("lastRun"),
        "jobs": jobs,
    }
    save_json(WEB_JOBS_FILE, payload)


def main() -> int:
    portals_cfg = load_json(PORTALS_FILE)
    filters = load_json(FILTERS_FILE)
    state = load_json(STATE_FILE) if STATE_FILE.exists() else {"jobs": [], "lastRun": None}

    enabled = [p for p in portals_cfg.get("portals", []) if p.get("enabled", True)]
    if not enabled:
        print("Žádné enabled portály v portals.json", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).astimezone()
    all_found: list[dict] = []
    failures: list[tuple[str, str]] = []

    for portal in enabled:
        jobs, err = scan_portal(portal, filters)
        if err:
            failures.append((portal["name"], err))
        filtered = filter_jobs(jobs, filters)
        all_found.extend(filtered)

    new_jobs, updated_jobs = update_state(state, all_found, now)
    save_json(STATE_FILE, state)
    export_web(state)

    report = build_report(new_jobs, updated_jobs, len(enabled), failures, now)
    print(report)
    print("\n---STATS---")
    print(json.dumps({
        "found": len(all_found),
        "new": len(new_jobs),
        "updated": len(updated_jobs),
        "failures": failures,
        "portals": len(enabled),
        "webExport": str(WEB_JOBS_FILE),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
