# Uranus – systemd deployment artefaktumok

Ez a könyvtár azokat a systemd drop-ineket tartalmazza, amelyeket a production
gépen telepíteni kell. **A repository eddig nem verziókövette a unit-fájlokat**,
így a runtime-topológia sem reviewálható, sem reprodukálható nem volt (U-2A
lelet). Ez a könyvtár ezt kezdi el orvosolni.

## Fontos hatókör-megkötés

Az itt lévő fájlok **specifikációk**, nem alkalmazott konfiguráció. Telepítésük
külön, dokumentált és jóváhagyott deploy-lépés. Az U-2B lokális köre semmilyen
production systemd állapotot nem módosított.

## Tartalom

| Fájl | Cél | Állapot |
|---|---|---|
| `uranus-runner.service.d/15-ordering.conf` | a runner ne induljon a Freqtrade előtt | **telepítésre vár** |

## A production drop-in sorrend (U-2A snapshot alapján)

A `uranus-runner.service` drop-injei ábécésorrendben olvasódnak be:

```
10-state-perms.conf     state.json létrehozás/jogosultság
15-ordering.conf        <-- ÚJ (U-2B Patch C)
20-ft-auth.conf         FT_URL + FT_USERNAME + FT_PASSWORD (mode 600)
30-canonical-env.conf   PAIR + FT_URL + FREQTRADE_RPC_URL + FT_API_URL
40-costs.conf           FEE_PCT + SLIPPAGE_PCT
70-shadow.conf          SHADOW_ENABLED + SHADOW_START_EQUITY_USDC
99-hardening.conf       User/Group/UMask/ProtectSystem/ReadWritePaths
```

A `15-ordering.conf` szándékosan csak `[Unit]` szekciót tartalmaz, így nem
ütközik egyetlen meglévő `[Service]` drop-innel sem.

## Amit ez a drop-in NEM old meg

- **Nem javítja a fail-open FLAT hibát.** Ha a Freqtrade futás közben esik ki,
  a pozíció-szinkron továbbra is „nincs pozíció”-ként értelmezi. Ez az U-3
  Position Authority feladata.
- **Nem old meg boot-autostartot.** Mindhárom Uranus unit `UnitFileState=disabled`,
  tehát újraindulás után kézi indítás kell. Ez tudatos üzemeltetői döntés lehet;
  ha meg akarjuk változtatni, az önálló, jóváhagyást igénylő lépés
  (`systemctl enable`), és előfeltétele, hogy az U-3 lezárult.
