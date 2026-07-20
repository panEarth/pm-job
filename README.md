# PM Job Monitor

Denní monitoring job portálů pro Product Manager pozice.

## Web přehled

Po zapnutí GitHub Pages: **https://panearth.github.io/pm-job/**

- nahoře **nové nabídky** z posledního běhu
- pod nimi **všechny dříve nalezené** pozice

Data: `docs/jobs.json` (aktualizuje se po každém běhu monitoru).

## Konfigurace

`.cursor/skills/pm-job-monitor/`

- `portals.json` — sledované portály
- `filters.json` — klíčová slova a lokace
- `state/seen-jobs.json` — historie inzerátů
- `monitor.py` — skript denního skenu + export na web
