---
name: pm-job-monitor
description: >-
  Monitoruje vybrané job portály a hledá nové pozice pro Product Managera
  (nebo podobné role). Porovnává výsledky s předchozími běhy, reportuje jen
  nové inzeráty. Použij při denním monitoringu pracovních nabídek, PM pozic,
  job alertů, nebo když uživatel zmíní job portály, Jobs.cz, LinkedIn Jobs,
  StartupJobs, Greenhouse, Lever apod.
---

# PM Job Monitor

Denní monitoring vybraných job portálů pro pozice Product Managera a podobné role.

## Konfigurace

Před každým během načti z `.cursor/skills/pm-job-monitor/` v repozitáři:

1. **Portály** — `portals.json` (vedle tohoto souboru)
2. **Stav** — `state/seen-jobs.json` (již viděné inzeráty)
3. **Filtry** — `filters.json` (klíčová slova, vyloučená slova, lokace)

Pokud `portals.json` obsahuje prázdný seznam `portals`, zastav se a požádej uživatele o doplnění.

## Klíčová slova (výchozí)

Hledej pozice obsahující (case-insensitive):

- product manager, product owner, head of product, vp product
- produktový manažer, produktový manažer/ka, vedoucí produktu
- senior pm, group pm, principal pm, staff pm
- product lead, product director

Vyluč pozice s: intern, internship, stáž, junior developer, software engineer (pokud není explicitně PM hybrid).

## Workflow

```
Task Progress:
- [ ] 1. Načíst konfiguraci a stav
- [ ] 2. Projít každý portál
- [ ] 3. Filtrovat relevantní pozice
- [ ] 4. Porovnat se stavem — vybrat jen NOVÉ
- [ ] 5. Aktualizovat state/seen-jobs.json
- [ ] 6. Exportovat web přehled (docs/jobs.json) a commitnout
- [ ] 7. Sloučit PR do main (publish.sh) — GitHub Pages servíruje jen z main
```

### Krok 1 — Načtení stavu

```bash
# Ověř, že existují potřebné soubory
ls -la portals.json filters.json state/seen-jobs.json 2>/dev/null || mkdir -p state
```

Pokud `state/seen-jobs.json` neexistuje, vytvoř: `{"jobs": [], "lastRun": null}`.

### Krok 2 — Prohledání portálů

Pro každý portál v `portals.json`:

**Typ `search_url`** — otevři předpřipravenou search URL:
```bash
agent-browser open "<searchUrl>" && agent-browser wait --load networkidle && agent-browser snapshot -i
```

**Typ `url`** — otevři stránku a vyhledej klíčová slova:
```bash
agent-browser open "<url>" && agent-browser wait --load networkidle
agent-browser find placeholder "Search" type "product manager"  # pokud existuje
# nebo použij eval pro extrakci odkazů
```

**Typ `rss`** — stáhni feed a parsuj XML/JSON (bez browseru):
```bash
curl -sL "<rssUrl>"
```

**Extrakce dat z browseru:**
```bash
agent-browser eval --stdin <<'EVALEOF'
JSON.stringify(
  Array.from(document.querySelectorAll('a[href*="job"], a[href*="position"], a[href*="career"], article, .job-card, [data-job-id]'))
    .slice(0, 50)
    .map(el => ({
      title: (el.querySelector('h2,h3,h4,.title') || el).textContent?.trim().slice(0, 200),
      url: el.href || el.querySelector('a')?.href,
      company: el.querySelector('.company, [class*="company"]')?.textContent?.trim(),
      location: el.querySelector('.location, [class*="location"]')?.textContent?.trim()
    }))
    .filter(j => j.title && j.url)
)
EVALEOF
```

Pokud `agent-browser` není dostupný, použij vestavěný browser MCP (`browser_navigate`, `browser_snapshot`) nebo `WebFetch` pro statické stránky/RSS.

**Poznámky k portálům:**
- Cookies/consent banner: klikni „Accept"/„Souhlasím" před extrakcí
- Stránkování: projdi max. 2 stránky na portál
- Login-required portály (`requiresAuth: true`): zaznamenej do reportu jako „vyžaduje přihlášení" a přeskoč
- **LinkedIn Jobs**: často login wall / anti-bot. Zkus veřejné výsledky bez loginu; pokud redirect na login nebo prázdný seznam → zaznamenej varování do logu běhu a pokračuj na další portál. Neukládej LinkedIn credentials.

### Krok 3 — Filtrování

Pro každou nalezenou pozici:
1. Ověř shodu s klíčovými slovy z `filters.json` (nebo výchozí seznam výše)
2. Ověř, že neobsahuje vyloučená slova
3. Normalizuj URL (odstraň tracking parametry: utm_*, ref, source)

### Krok 4 — Detekce nových

Unikátní ID pozice = `normalize(url)` nebo `hash(company + title)`.

Porovnej s `state/seen-jobs.json`:
- **Nová** = ID není v `jobs[]`
- **Aktualizovaná** = stejné ID, jiný title (označ jako „aktualizováno")

### Krok 5 — Aktualizace stavu

Přidej všechny dnešní nalezené pozice do `seen-jobs.json`:

```json
{
  "jobs": [
    {
      "id": "startupjobs-cs-product-manager-12345",
      "title": "Senior Product Manager",
      "company": "Acme",
      "location": "Praha / Remote",
      "url": "https://...",
      "portal": "StartupJobs",
      "firstSeen": "2026-07-15",
      "lastSeen": "2026-07-15"
    }
  ],
  "lastRun": "2026-07-15T09:00:00+02:00"
}
```

Pravidla údržby stavu:
- Aktualizuj `lastSeen` u existujících pozic
- Smaž záznamy starší než 90 dní (pozice už pravděpodobně nejsou aktivní)
- Max. 2000 záznamů — při překročení smaž nejstarší

### Krok 5b — Web přehled

Po aktualizaci stavu exportuj data pro stránku `docs/index.html`:

1. Zapiš `docs/jobs.json` ve tvaru:
   ```json
   { "lastRun": "...", "generatedAt": "...", "jobs": [ ... ] }
   ```
2. Seřaď `jobs` od nejnovějších (`firstSeen` / `lastSeen`)
3. Commitni a pushni `state/seen-jobs.json` + `docs/jobs.json`

### Krok 5c — Publikace na GitHub Pages

GitHub Pages servíruje z větve **main** (`/docs`). Změny na feature branchi web neaktualizují.

Po commitu a pushi:

1. Vytvoř PR přes `open_git_pr` MCP (nebo `gh pr create`)
2. Spusť merge do main:

```bash
bash .cursor/skills/pm-job-monitor/publish.sh
# nebo s číslem PR: publish.sh 5
```

Skript označí draft PR jako ready, sloučí do main a počká na pages deploy.

Sekce stránky:
- **Nové nabídky** = `firstSeen` == datum z `lastRun`
- **Dříve nalezené** = ostatní

Pokud běží `monitor.py`, export provede automaticky funkce `export_web()`.

### Krok 6 — Shrnutí běhu (bez Slacku)

**Neposílej Slack zprávy.** Report je web přehled + krátký log.

Do logu běhu vypiš:

```markdown
PM Job Monitor — [datum]

Nové: X · Celkem ve stavu: N · Portály: M
Web: https://panearth.github.io/pm-job/
```

Pokud nějaký portál selhal, přidej:
```markdown
Varování:
- [Portál]: [důvod]
```

## Přidání nového portálu

Uživatel doplní záznam do `portals.json`:

```json
{
  "name": "StartupJobs",
  "type": "search_url",
  "searchUrl": "https://www.startupjobs.cz/nabidky/product-manager",
  "notes": "Český startup portál"
}
```

Podporované typy: `search_url`, `url`, `rss`.

## Chybové stavy

| Situace | Akce |
|---------|------|
| Portál nedostupný (timeout/403) | Zaznamenej, pokračuj na další |
| Prázdný výsledek | OK — žádné pozice na portálu |
| Změna layoutu portálu | Zkus alternativní selektory, upozorni v logu |
| Login required | Přeskoč, navrhni přidání do `portals.json` s `requiresAuth: true` |

## Bezpečnost

- Neukládej přihlašovací údaje do konfigurace
- Respektuj robots.txt; nepřetěžuj portály (max 1 request/sekunda)
