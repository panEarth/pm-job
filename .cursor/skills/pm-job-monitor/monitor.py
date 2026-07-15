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
sys.path.insert(0, str(BASE_DIR))

from api_scraper import fetch_adzuna_api, fetch_himalayas_api, fetch_jooble_api  # noqa: E402
from browser_scraper import BrowserScraper  # noqa: E402
PORTALS_FILE = BASE_DIR / "portals.json"
FILTERS_FILE = BASE_DIR / "filters.json"
STATE_FILE = BASE_DIR / "state" / "seen-jobs.json"

USER_AGENT = "Mozilla/5.0 (compatible; PMJobMonitor/1.0)"
REQUEST_DELAY = 1.0

PORTAL_STYLE = {
    "StartupJobs.cz": {"emoji": "🚀", "label": "StartupJobs.cz", "bar": "🟠"},
    "Jobs.cz": {"emoji": "💼", "label": "Jobs.cz", "bar": "🔵"},
    "NoFluffJobs CZ": {"emoji": "🧩", "label": "NoFluffJobs", "bar": "🟢"},
    "Indeed CZ": {"emoji": "🔎", "label": "Indeed CZ", "bar": "🟣"},
    "Jooble CZ": {"emoji": "📋", "label": "Jooble CZ", "bar": "🟡"},
    "Tribee": {"emoji": "🐝", "label": "Tribee", "bar": "🟤"},
    "Himalayas": {"emoji": "🏔️", "label": "Himalayas", "bar": "🔷"},
}
PORTAL_ORDER = ["StartupJobs.cz", "Jobs.cz", "NoFluffJobs CZ", "Himalayas", "Indeed CZ", "Jooble CZ", "Tribee"]
BROWSER_PORTALS = {"StartupJobs.cz", "Tribee", "Indeed CZ", "Jooble CZ"}


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


def location_ok(location: str, title: str, filters: dict) -> bool:
    if not filters.get("locationRequired", True):
        return True
    text = f"{location} {title}".lower()
    accept = [k.lower() for k in filters["locations"]["accept"]]
    remote_eu = [k.lower() for k in filters["locations"]["remoteAlsoAccept"]]
    if any(k in text for k in accept):
        return True
    if any(k in text for k in remote_eu):
        return True
    if "remote" in text:
        return True
    # Jobs.cz CZ listings often lack explicit location in card — keep PM titles
    return True


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


def scan_portal(portal: dict, filters: dict, browser_jobs: dict[str, list[dict]] | None = None) -> tuple[list[dict], str | None]:
    name = portal["name"]
    ptype = portal["type"]
    browser_jobs = browser_jobs or {}

    if name in browser_jobs:
        return browser_jobs[name], None

    if name in BROWSER_PORTALS:
        return [], None

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

    if name == "Himalayas" or ptype == "api":
        params = portal.get("apiParams", {})
        return fetch_himalayas_api(
            name,
            query=params.get("q", "product manager"),
            country=params.get("country", "Czechia"),
        )

    if ptype == "search_url":
        content, err = fetch(portal["searchUrl"])
        time.sleep(REQUEST_DELAY)
        if err:
            return [], err
        return [], "Nepodporovaný portál bez parseru"

    return [], "Neznámý typ portálu"


def slack_link(url: str, label: str) -> str:
    safe_label = label.replace("|", "·").replace(">", "›").replace("<", "‹")
    return f"<{url}|{safe_label}>"


def portal_header(portal: str, count: int) -> str:
    style = PORTAL_STYLE.get(portal, {"emoji": "📌", "label": portal, "bar": "⚪"})
    return f"{style['bar']} *{style['emoji']} {style['label']}* — {count} {'pozice' if count == 1 else 'pozic'}"


def format_job_line(index: int, job: dict) -> list[str]:
    company = job.get("company") or "Neznámá firma"
    location = job.get("location") or "Neuvedeno"
    url = job.get("url", "")
    title = job.get("title", "")
    link = slack_link(url, title) if url else title
    return [
        f"*{index}. {link}*",
        f"   🏢 {company}  ·  📍 {location}",
    ]


def dedupe_key(job: dict) -> str:
    title = re.sub(r"\s+", " ", job.get("title", "").strip().lower())
    company = re.sub(r"\s+", " ", job.get("company", "").strip().lower())
    url = job.get("url", "")
    if "nofluffjobs.com/job/" in url:
        slug = url.rsplit("/", 1)[-1]
        slug = re.sub(
            r"-(?:remote|warszawa|warsaw|krakow|kraków|wroclaw|wrocław|poznan|poznań|gdansk|gdańsk|"
            r"katowice|lodz|łódź|lublin|sopot|gorzowwielkopolski|lower-silesian|kuyavian-pomeranian|"
            r"lesser-poland|masovian|lubusz|opole|subcarpathian|podlaskie|pomeranian|silesian|"
            r"holy-cross|warmian-masurian|greater-poland|west-pomeranian|pl|cz)(?:-\d+)?$",
            "",
            slug,
            flags=re.I,
        )
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
            rec["company"] = job.get("company") or rec.get("company", "")
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
    fallback_notes: list[tuple[str, str]] | None = None,
) -> str:
    date_str = now.strftime("%d.%m.%Y")
    time_str = now.strftime("%H:%M")
    fallback_notes = fallback_notes or []
    report_items = new_jobs + [
        {**u, "title": f"{u['title']} (aktualizováno z: {u.get('oldTitle', '')})"}
        for u in updated_jobs
    ]

    lines: list[str] = [f"🔍 *PM Job Monitor — {date_str}*"]

    if failures:
        lines.extend(["", "⚠️ *Varování:*"])
        for portal, reason in failures:
            style = PORTAL_STYLE.get(portal, {"emoji": "⚠️"})
            lines.append(f"   {style.get('emoji', '⚠️')} *{portal}*: {reason}")

    if fallback_notes:
        lines.extend(["", "ℹ️ *Fallback zdroje:*"])
        for portal, note in fallback_notes:
            style = PORTAL_STYLE.get(portal, {"emoji": "ℹ️"})
            lines.append(f"   {style.get('emoji', 'ℹ️')} *{portal}*: {note}")

    if report_items:
        lines.extend(["", f"Nalezeno *{len(report_items)} nových* pozic:", ""])
        by_portal: dict[str, list[dict]] = {}
        for job in report_items:
            by_portal.setdefault(job.get("portal", "?"), []).append(job)

        first = True
        for portal in PORTAL_ORDER:
            items = by_portal.pop(portal, [])
            if not items:
                continue
            if not first:
                lines.append("────────────────────")
            first = False
            lines.append(portal_header(portal, len(items)))
            lines.append("")
            for i, job in enumerate(items, 1):
                lines.extend(format_job_line(i, job))
                lines.append("")

        for portal, items in by_portal.items():
            if not first:
                lines.append("────────────────────")
            lines.append(portal_header(portal, len(items)))
            lines.append("")
            for i, job in enumerate(items, 1):
                lines.extend(format_job_line(i, job))
                lines.append("")

        lines.append(f"_Celkem monitorováno: {portal_count} portálů · Poslední běh: {time_str}_")
    elif not failures:
        lines.extend(["", f"✅ Žádné nové PM pozice. Monitorováno {portal_count} portálů."])
    else:
        lines.append(f"_Celkem monitorováno: {portal_count} portálů · Poslední běh: {time_str}_")

    return "\n".join(lines).strip()


def scrape_browser_portals(portals: list[dict]) -> tuple[dict[str, list[dict]], list[tuple[str, str]], list[tuple[str, str]]]:
    enabled = {p["name"] for p in portals}
    jobs: dict[str, list[dict]] = {}
    failures: list[tuple[str, str]] = []
    fallback_notes: list[tuple[str, str]] = []

    try:
        with BrowserScraper() as scraper:
            if "StartupJobs.cz" in enabled:
                portal = next(p for p in portals if p["name"] == "StartupJobs.cz")
                jobs["StartupJobs.cz"] = scraper.scrape_startupjobs(portal["searchUrl"], "StartupJobs.cz")
            if "Tribee" in enabled:
                portal = next(p for p in portals if p["name"] == "Tribee")
                url = portal.get("searchUrl") or f"{portal['url']}?q=product+manager"
                jobs["Tribee"] = scraper.scrape_tribee(url, "Tribee")
            if "Indeed CZ" in enabled:
                portal = next(p for p in portals if p["name"] == "Indeed CZ")
                indeed_jobs, err = scraper.scrape_indeed(portal["searchUrl"], "Indeed CZ")
                if indeed_jobs:
                    jobs["Indeed CZ"] = indeed_jobs
                else:
                    # Indeed security redirect poškozuje session — vyčistit cookies
                    try:
                        scraper.context.clear_cookies()
                    except Exception:  # noqa: BLE001
                        pass
                    linkedin_jobs = scraper.scrape_linkedin_fallback("Indeed CZ")
                    if linkedin_jobs:
                        jobs["Indeed CZ"] = linkedin_jobs
                        fallback_notes.append((
                            "Indeed CZ",
                            f"přímý scraping blokován → LinkedIn fallback ({len(linkedin_jobs)} pozic)",
                        ))
                    else:
                        api_jobs, api_err = fetch_adzuna_api("Indeed CZ")
                        if api_jobs:
                            jobs["Indeed CZ"] = api_jobs
                            fallback_notes.append((
                                "Indeed CZ",
                                f"přímý scraping blokován → Adzuna API ({len(api_jobs)} pozic)",
                            ))
                        else:
                            failures.append(("Indeed CZ", err or api_err or "Všechny fallbacky selhaly"))
            if "Jooble CZ" in enabled:
                portal = next(p for p in portals if p["name"] == "Jooble CZ")
                jooble_jobs, err = scraper.scrape_jooble(portal["searchUrl"], "Jooble CZ")
                if jooble_jobs:
                    jobs["Jooble CZ"] = jooble_jobs
                elif err:
                    api_jobs, api_err = fetch_jooble_api("Jooble CZ")
                    if api_jobs:
                        jobs["Jooble CZ"] = api_jobs
                        fallback_notes.append((
                            "Jooble CZ",
                            f"browser blokován → Jooble REST API ({len(api_jobs)} pozic)",
                        ))
                    else:
                        adzuna_jobs, adzuna_err = fetch_adzuna_api("Jooble CZ")
                        if adzuna_jobs:
                            jobs["Jooble CZ"] = adzuna_jobs
                            fallback_notes.append((
                                "Jooble CZ",
                                f"browser blokován → Adzuna API ({len(adzuna_jobs)} pozic)",
                            ))
                        else:
                            reason = api_err or adzuna_err or err or "Všechny fallbacky selhaly"
                            failures.append(("Jooble CZ", reason))
    except Exception as exc:  # noqa: BLE001
        for name in ["StartupJobs.cz", "Tribee", "Indeed CZ", "Jooble CZ"]:
            if name in enabled and name not in jobs and not any(f[0] == name for f in failures):
                failures.append((name, str(exc)))

    return jobs, failures, fallback_notes


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
    browser_jobs, browser_failures, fallback_notes = scrape_browser_portals(enabled)
    failures: list[tuple[str, str]] = list(browser_failures)

    for portal in enabled:
        jobs, err = scan_portal(portal, filters, browser_jobs)
        if err:
            failures.append((portal["name"], err))
        filtered = filter_jobs(jobs, filters)
        all_found.extend(filtered)

    # deduplicate failure messages per portal
    seen_fail: set[str] = set()
    unique_failures: list[tuple[str, str]] = []
    for portal, reason in failures:
        if portal in seen_fail:
            continue
        seen_fail.add(portal)
        unique_failures.append((portal, reason))
    failures = unique_failures

    new_jobs, updated_jobs = update_state(state, all_found, now)
    save_json(STATE_FILE, state)

    report = build_report(new_jobs, updated_jobs, len(enabled), failures, now, fallback_notes)
    print(report)
    print("\n---STATS---")
    print(json.dumps({
        "found": len(all_found),
        "new": len(new_jobs),
        "updated": len(updated_jobs),
        "failures": failures,
        "fallback_notes": fallback_notes,
        "portals": len(enabled),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
