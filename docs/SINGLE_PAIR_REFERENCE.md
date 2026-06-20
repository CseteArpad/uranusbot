# Uranus – Single-pair reference implementation (LEZÁRVA)

Cél: stabil, auditálható „single-pair” referencia, amire később a multi-pair bővítés épülhet.

Architektúra:
- **Freqtrade = motor** (trade process + API a config.json alapján)
- **Uranus UI = döntésvizualizáció** (6 szabály állapota + célárak + kiemelések)

## 0) Projekt-szintű aranyszabályok (Uranus / Freqtrade + API – B opció)
- 1 bot = 1 systemd service = 1 freqtrade trade process (**nincs külön api.service**).
- API **kizárólag** a `config.json`-ból indul (nincs kézi uvicorn/külön API indítás).
- **Nincs kézi** `freqtrade trade ...` futtatás, ha a service fut.
- **Nincs rutin** `pkill -9`.
- Port/bind módosítást **csak service stop után**.
- Debug alatt **nincs** `Restart=always`.
- JWT secret **nem maradhat default**.

## 1) Futtatott komponensek
Systemd service-ek:
- `uranus-freqtrade.service`  → Freqtrade motor (engine + API via config, dry_run vagy live)
- `uranus-ui.service`         → Uranus UI (6 rules decision dashboard)

Ellenőrzés:
```bash
systemctl list-units --type=service | grep -i uranus
systemctl status uranus-freqtrade.service --no-pager
systemctl status uranus-ui.service --no-pager

