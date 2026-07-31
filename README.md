# PM Job Monitor

Denní monitoring job portálů pro Product Manager pozice.

## Web přehled

**https://panearth.github.io/pm-job/**

- nahoře **nové nabídky** z posledního běhu
- pod nimi **všechny dříve nalezené** pozice

Žádné Slack notifikace — jediný výstup je tato stránka (+ log běhu automatizace).

Data: `docs/jobs.json` (aktualizuje se po každém běhu monitoru).

GitHub Pages servíruje z větve **main**. Po každém běhu automatizace se PR automaticky sloučí přes `publish.sh`.

## Spuštění (lokálně i v cloudové automatizaci)

```bash
bash .cursor/skills/pm-job-monitor/run-monitor.sh
```

Skript vytvoří `.venv`, nainstaluje `requirements.txt` (Playwright) a Chromium, pak spustí `monitor.py`.
**`.venv` necommituj** — je platformově specifické (macOS ≠ Linux cloud); na každém běhu se znovu připraví ze `requirements.txt`.

## Konfigurace

`.cursor/skills/pm-job-monitor/`

- `portals.json` — sledované portály
- `filters.json` — klíčová slova a lokace
- `state/seen-jobs.json` — historie inzerátů
- `monitor.py` — skript denního skenu + export na web
- `run-monitor.sh` — venv + Playwright + spuštění monitoru
- `automation-prompt.txt` — prompt pro Cursor Automation
- `secrets.local.json` — lokální API klíče (gitignore; viz `secrets.local.json.example`)
  - `JOOBLE_API_KEY` — Jooble REST API (nebo env stejného jména)