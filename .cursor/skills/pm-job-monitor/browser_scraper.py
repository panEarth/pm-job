"""Headless browser scraping (Playwright + izolovaný profil)."""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Browser, Page, sync_playwright

BASE_URL = "https://www.startupjobs.cz"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]


class BrowserScraper:
    def __init__(self) -> None:
        self._profile_dir = Path(tempfile.mkdtemp(prefix="pm-job-chrome-"))
        self._playwright = None
        self._browser: Browser | None = None

    def __enter__(self) -> BrowserScraper:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir),
            headless=True,
            args=BROWSER_ARGS,
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="cs-CZ",
        )
        return self

    def __exit__(self, *args: object) -> None:
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        shutil.rmtree(self._profile_dir, ignore_errors=True)

    @property
    def context(self):
        if not self._browser:
            raise RuntimeError("Browser not started")
        return self._browser

    def _page(self) -> Page:
        return self.context.new_page()

    def _goto(self, page: Page, url: str, wait_ms: int = 3000) -> None:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)

    def _click_cookie_banners(self, page: Page) -> None:
        selectors = [
            "#onetrust-accept-btn-handler",
            "button:has-text('Přijmout')",
            "button:has-text('Souhlasím')",
            "button:has-text('Accept all')",
            "button:has-text('Accept')",
        ]
        for sel in selectors:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click(timeout=3000)
                    page.wait_for_timeout(1000)
                    return
            except Exception:  # noqa: BLE001
                continue

    def scrape_startupjobs(self, search_url: str, portal: str, max_pages: int = 2) -> list[dict]:
        jobs: list[dict] = []
        page = self._page()
        try:
            for page_num in range(1, max_pages + 1):
                url = search_url if page_num == 1 else f"{search_url}?page={page_num}"
                self._goto(page, url, wait_ms=4000)
                self._click_cookie_banners(page)
                cards = page.query_selector_all('a[href*="/nabidka/"]')
                best_text: dict[str, str] = {}
                for card in cards:
                    href = card.get_attribute("href") or ""
                    if not href or "/nabidka/" not in href:
                        continue
                    full_url = href if href.startswith("http") else urljoin(BASE_URL, href)
                    norm = full_url.split("?")[0]
                    raw = card.inner_text().strip()
                    if not raw:
                        continue
                    best_text[norm] = _best_card_text(best_text.get(norm), raw)

                for norm, raw in best_text.items():
                    parsed = _parse_startupjobs_card(raw)
                    if not parsed:
                        continue
                    if not parsed.get("company"):
                        parsed["company"] = _company_from_detail_page(page, norm)
                    jobs.append({
                        "title": parsed["title"],
                        "company": parsed["company"],
                        "location": parsed["location"],
                        "url": norm,
                        "portal": portal,
                    })
        finally:
            page.close()
        return jobs

    def scrape_tribee(self, search_url: str, portal: str, max_pages: int = 2) -> list[dict]:
        jobs: list[dict] = []
        page = self._page()
        try:
            self._goto(page, search_url, wait_ms=4000)
            self._click_cookie_banners(page)
            search = page.query_selector('input[type="search"], input[placeholder*="prac"], input[name="q"]')
            if search:
                search.fill("product manager")
                search.press("Enter")
                page.wait_for_timeout(4000)

            for _ in range(max_pages):
                links = page.eval_on_selector_all(
                    "a[href*='/prace/']",
                    """els => els.map(e => ({
                        href: e.href,
                        text: e.innerText.trim()
                    })).filter(x => x.text.length > 5)""",
                )
                seen: set[str] = set()
                for link in links:
                    href = link.get("href", "")
                    if "/cs/spolecnost/" not in href and "/cs/prace/" not in href:
                        continue
                    if href in seen:
                        continue
                    seen.add(href)
                    parsed = _parse_tribee_link(link.get("text", ""))
                    if not parsed:
                        continue
                    jobs.append({
                        "title": parsed["title"],
                        "company": parsed["company"],
                        "location": parsed["location"],
                        "url": href,
                        "portal": portal,
                    })

                next_btn = page.query_selector('a[rel="next"], button:has-text("Další"), a:has-text("Další")')
                if not next_btn:
                    break
                try:
                    next_btn.click()
                    page.wait_for_timeout(3000)
                except Exception:  # noqa: BLE001
                    break
        finally:
            page.close()
        return jobs

    def scrape_indeed(self, search_url: str, portal: str, max_pages: int = 2) -> tuple[list[dict], str | None]:
        jobs: list[dict] = []
        page = self._page()
        try:
            self._goto(page, search_url, wait_ms=5000)
            self._click_cookie_banners(page)
            page.wait_for_timeout(3000)

            if "blocked" in page.title().lower():
                return [], "Indeed blokuje headless browser (Blocked)"

            for _ in range(max_pages):
                cards = page.query_selector_all(".job_seen_beacon, .result")
                if not cards:
                    return [], "Nepodařilo se načíst výsledky (prázdná stránka)"

                for card in cards:
                    title_el = card.query_selector("h2 a, .jcs-JobTitle, a[data-jk]")
                    if not title_el:
                        continue
                    title = title_el.inner_text().strip()
                    href = title_el.get_attribute("href") or ""
                    if href and not href.startswith("http"):
                        href = urljoin("https://cz.indeed.com", href)
                    company_el = card.query_selector(
                        '[data-testid="company-name"], .companyName, [data-testid="attribute_snippet_testid"]'
                    )
                    loc_el = card.query_selector('[data-testid="text-location"], .companyLocation')
                    jobs.append({
                        "title": title,
                        "company": company_el.inner_text().strip() if company_el else "",
                        "location": loc_el.inner_text().strip() if loc_el else "",
                        "url": href,
                        "portal": portal,
                    })

                next_link = page.query_selector('a[aria-label="Next Page"], a[aria-label="Další"]')
                if not next_link:
                    break
                next_link.click()
                page.wait_for_timeout(3000)
        finally:
            page.close()
        return jobs, None

    def scrape_jooble(self, search_url: str, portal: str, max_pages: int = 2) -> tuple[list[dict], str | None]:
        page = self._page()
        try:
            self._goto(page, search_url, wait_ms=5000)
            for _ in range(18):
                if "just a moment" not in page.title().lower():
                    break
                page.wait_for_timeout(5000)

            if "just a moment" in page.title().lower():
                return [], "Cloudflare ochrana — headless browser neprošel"

            jobs: list[dict] = []
            for _ in range(max_pages):
                items = page.query_selector_all('a[href*="/desc/"]')
                seen: set[str] = set()
                for item in items:
                    href = item.get_attribute("href") or ""
                    if not href or href in seen:
                        continue
                    seen.add(href)
                    title = item.inner_text().strip().split("\n")[0]
                    jobs.append({
                        "title": title,
                        "company": "",
                        "location": "",
                        "url": href if href.startswith("http") else urljoin("https://cz.jooble.org", href),
                        "portal": portal,
                    })
                next_btn = page.query_selector('a[rel="next"], button:has-text("Další")')
                if not next_btn:
                    break
                next_btn.click()
                page.wait_for_timeout(3000)
            if not jobs:
                return [], "Cloudflare ochrana — žádné výsledky (pravděpodobně blokace)"
            return jobs, None
        finally:
            page.close()


def _company_from_detail_page(page: Page, url: str) -> str:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        link = page.query_selector('a[href*="/startup/"]')
        if not link:
            return ""
        href = link.get_attribute("href") or ""
        slug = href.rstrip("/").split("/")[-1]
        if slug:
            return slug.replace("-", " ").title()
        return link.inner_text().strip().split("\n")[0].strip()
    except Exception:  # noqa: BLE001
        return ""


def _parse_startupjobs_card(raw: str) -> dict | None:
    normalized = raw.replace(" | ", "\n")
    parts = [p.strip() for p in normalized.split("\n") if p.strip()]
    if len(parts) >= 3:
        company, title, location = parts[0], parts[1], parts[2]
        if title.lower() in {"full-time", "part-time", "remote", "hybrid", "onsite"}:
            return None
        return {"company": company, "title": title, "location": location}

    # Fallback: sloučený text bez newline
    m = re.match(
        r"^(.+?)\s+(Product Manager.+?|Product Owner.+?|Produktov.+?|Head of Product.+?)"
        r"(?:\s*(Praha|Brno|Remote|Hybrid|Onsite).*)?$",
        raw,
        re.I,
    )
    if m:
        return {"company": m.group(1).strip(), "title": m.group(2).strip(), "location": (m.group(3) or "").strip()}
    return None


def _best_card_text(current: str | None, new: str) -> str:
    if not current:
        return new
    return new if new.count("\n") >= current.count("\n") else current


def _parse_tribee_link(text: str) -> dict | None:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return None
    title = lines[0]
    company = lines[1] if len(lines) > 1 else ""
    location = lines[2] if len(lines) > 2 else ""
    return {"title": title, "company": company, "location": location}
