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
PORTALS_FILE = BASE_DIR / "portals.json"
FILTERS_FILE = BASE_DIR / "filters.json"
STATE_FILE = BASE_DIR / "state" / "seen-jobs.json"

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


def job_id(url: str, title: str, company: str) -> str:
    norm = normalize_url(url)
    if norm and norm != "https://www.jobs.cz":
        return hashlib.sha256(norm.encode()).hexdigest()[:16]
    key = f"{company}|{title}".strip().lower()
    return hashlib.sha256(key.encode()).hexdigest()[:16]


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

EU_LOCATION_HINTS = [
    "poland", "warszawa", "warsaw", "kraków", "krakow", "wrocław", "wroclaw",
    "poznań", "poznan", "gdańsk", "gdansk", "sopot", "bratislava", "slovakia",
    "germany", "berlin", "munich", "austria", "vienna", "hungary", "budapest",
]


def location_ok(location: str, title: str, filters: dict) -> bool:
    if not filters.get("locationRequired", True):
        return True
    loc = (location or "").strip()
    text = f"{loc} {title}".lower()
    reject = [k.lower() for k in filters.get("rejectLocations", REJECT_LOCATIONS)]
    if loc and any(k in text for k in reject):
        return False
    accept = [k.lower() for k in filters["locations"]["accept"]]
    remote_eu = [k.lower() for k in filters["locations"]["remoteAlsoAccept"]]
    if any(k in text for k in accept):
        return True
    if any(k in text for k in remote_eu):
        return True
    if any(k in text for k in EU_LOCATION_HINTS):
        return True
    if "remote" in text:
        return True
    # Jobs.cz CZ listings often lack explicit location in card — keep PM titles
    if not loc:
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
        company_m = re.search(r'alt="([^"]+)"', article)
        company = company_m.group(1).strip() if company_m else ""
        loc_m = re.search(r'data-test="location"[^>]*>([^<]+)<', article)
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
        title = title[:-len("| StartupJobs.cz")].strip()

    company = ""
    for pat in [
        r'"company"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
        r'"companyName"\s*:\s*"([^"]+)"',
        r'itemprop="hiringOrganization"[^>]*>[\s\S]*?itemprop="name"[^>]*>([^<]+)<',
        r'itemprop="name"[^>]*>([^<]+)<',
    ]:
        m = re.search(pat, html_text)
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
    ]
    return [u for u in urls if any(k in u.lower() for k in keys)]


def parse_nofluffjobs(posting: dict, portal: str) -> dict:
    loc = posting.get("location", {})
    places = loc.get("places", [])
    parts = []
    for pl in places:
        city = pl.get("city", "")
        country = pl.get("country", {}).get("name", "")
        if city:
            parts.append(city)
        if country:
            parts.append(country)
    if posting.get("fullyRemote"):
        parts.append("Remote")
    location = ", ".join(dict.fromkeys(parts))
    return {
        "title": posting.get("title", "").strip(),
        "company": posting.get("name", "").strip(),
        "location": location,
        "url": f"https://nofluffjobs.com/job/{posting.get('url', '')}",
        "portal": portal,
    }


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
    accept = [k.lower() for k in filters["locations"]["accept"]]
    remote_eu = [k.lower() for k in filters["locations"]["remoteAlsoAccept"]]
    if "cz" in (posting.get("regions") or []):
        return True
    if any(k in text for k in accept):
        return True
    if posting.get("fullyRemote") and (any(k in text for k in remote_eu) or not places):
        return True
    return False


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


def dedupe_key(job: dict) -> str:
    title = re.sub(r"\s+", " ", job.get("title", "").strip().lower())
    company = re.sub(r"\s+", " ", job.get("company", "").strip().lower())
    url = job.get("url", "")
    if "nofluffjobs.com/job/" in url:
        slug = url.rsplit("/", 1)[-1]
        slug = re.sub(r"-(?:remote|warszawa|warsaw|krakow|kraków|wroclaw|wrocław|poznan|poznań|gdansk|gdańsk|katowice|lodz|łódź|lublin|pl|cz)(?:-\d+)?$", "", slug, flags=re.I)
        return f"nofluff|{company}|{title}|{slug}"
    norm = normalize_url(url)
    if norm:
        return f"url|{norm}"
    return f"text|{company}|{title}"


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
            continue
        seen.add(key)
        job["url"] = normalize_url(job.get("url", "")) or job.get("url", "")
        filtered.append(job)
    return filtered


def update_state(state: dict, found_jobs: list[dict], now: datetime) -> tuple[list[dict], list[dict]]:
    today = now.date().isoformat()
    existing = {j["id"]: j for j in state.get("jobs", [])}
    new_jobs = []
    updated_jobs = []

    for job in found_jobs:
        jid = job_id(job.get("url", ""), job["title"], job.get("company", ""))
        if jid in existing:
            rec = existing[jid]
            old_title = rec.get("title", "")
            rec["lastSeen"] = today
            rec["title"] = job["title"]
            rec["company"] = job.get("company", rec.get("company", ""))
            rec["location"] = job.get("location", rec.get("location", ""))
            rec["url"] = job.get("url", rec.get("url", ""))
            rec["portal"] = job.get("portal", rec.get("portal", ""))
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
            new_jobs.append(rec)

    cutoff = (now.date() - timedelta(days=90)).isoformat()
    jobs_list = [j for j in existing.values() if j.get("lastSeen", "1970-01-01") >= cutoff]
    jobs_list.sort(key=lambda x: x.get("lastSeen", ""), reverse=True)
    jobs_list = jobs_list[:2000]

    state["jobs"] = jobs_list
    state["lastRun"] = now.isoformat()
    return new_jobs, updated_jobs


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

    report = build_report(new_jobs, updated_jobs, len(enabled), failures, now)
    print(report)
    print("\n---STATS---")
    print(json.dumps({
        "found": len(all_found),
        "new": len(new_jobs),
        "updated": len(updated_jobs),
        "failures": failures,
        "portals": len(enabled),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
