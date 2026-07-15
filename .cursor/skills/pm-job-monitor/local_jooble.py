#!/usr/bin/env python3
"""Jooble CZ scraper pro spuštění z domácí sítě (residential IP).

Cloudflare typicky blokuje datacentrové IP (Cursor Cloud Agent).
Z domácího počítače spusť:

    cd .cursor/skills/pm-job-monitor
    pip install -r requirements.txt
    python -m playwright install chromium
    python local_jooble.py

Výsledek se uloží do state/jooble-local-cache.json a cloud monitor ho načte
při dalším denním běhu (platnost 36 hodin).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from api_scraper import fetch_jooble_cz_html, save_jooble_local_cache  # noqa: E402
from browser_scraper import BrowserScraper  # noqa: E402

PORTALS_FILE = BASE_DIR / "portals.json"
CACHE_FILE = BASE_DIR / "state" / "jooble-local-cache.json"


def load_portal() -> dict:
    with PORTALS_FILE.open(encoding="utf-8") as f:
        portals = json.load(f).get("portals", [])
    for portal in portals:
        if portal.get("name") == "Jooble CZ":
            return portal
    raise SystemExit("Jooble CZ není v portals.json")


def main() -> int:
    portal = load_portal()
    search_url = portal["searchUrl"]
    keywords = portal.get("searchQuery", "product manager")
    jobs: list[dict] = []
    method = ""

    print(f"Scrapuji Jooble CZ: {search_url}")
    try:
        with BrowserScraper() as scraper:
            jobs, err = scraper.scrape_jooble(search_url, "Jooble CZ")
            if jobs:
                method = "browser (domácí IP)"
            elif err:
                print(f"Browser: {err}")
    except Exception as exc:  # noqa: BLE001
        print(f"Browser selhal: {exc}")

    if not jobs:
        jobs, err = fetch_jooble_cz_html("Jooble CZ", listing_url=search_url, keywords=keywords)
        if jobs:
            method = "html fetch (domácí IP)"
        elif err:
            print(f"HTML fetch: {err}")

    if not jobs:
        print("\n❌ Žádné pozice — Cloudflare stále blokuje, nebo na stránce nejsou /jdp/ odkazy.")
        return 1

    now = datetime.now(timezone.utc).astimezone()
    save_jooble_local_cache(jobs, method=method, fetched_at=now)
    print(f"\n✅ Uloženo {len(jobs)} pozic → {CACHE_FILE}")
    print(f"   Metoda: {method}")
    print(f"   Cache platí 36 h — cloud monitor ji použije při dalším běhu.")
    for job in jobs[:5]:
        print(f"   · {job.get('title', '?')[:60]}")
    if len(jobs) > 5:
        print(f"   … a dalších {len(jobs) - 5}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
