# OWNER EXECUTION VENUE DECISION — 2026-09-01

```
PRIMARY_EXECUTION_VENUE       = OKX_SPOT
BINANCE_LIVE_EXECUTION        = PERMANENTLY_RETIRED
BINANCE_FALLBACK              = FORBIDDEN
BINANCE_RESEARCH_PROVENANCE   = RETAINED
OKX_LIVE_EXECUTION_AUTHORIZED = NO
```

## A döntés jellege

**Ez tulajdonosi architekturális döntés, NEM empirikus stratégiai verdikt.**

Nem abból következik, hogy a Binance mérhetően rosszabb végrehajtási helyszín
lenne, és nem támaszkodik semmilyen kutatási eredményre. Nem hivatkozik a
C1/SH-4 belépési hipotézisre, nem használ `RESERVED_UNSEEN` adatot, és nem
minősít felül egyetlen tudományos kaput sem.

Amit kimond: **az UranusBot végrehajtási felülete egyetlen helyszínre szűkül,
és a többi út véglegesen lezárul.**

| | |
|---|---|
| Döntéshozó | a rendszer tulajdonosa |
| Dátum | 2026-09-01 |
| Hatálya | UranusBot production végrehajtás |
| Kiváltó lelet | `URANUS_PRODUCTION_IDENTITY_AUDIT_2026-09-01.md` |
| Visszavonhatóság | kódváltoztatás + külön tulajdonosi döntés + review |

## Mit tilt

1. **Éles Binance végrehajtás** – rendelés küldése, módosítása, visszavonása
   bármely Binance végponton, bármely úton (Freqtrade, adapter, közvetlen
   aláírt REST).
2. **Binance mint fallback** – ha az OKX-út nem elérhető, a rendszer
   **elutasít**, nem vált másik tőzsdére. Nincs alapértelmezett helyszín.
3. **Binance kereskedési hitelesítő adat** a production fában.

## Mit NEM tilt

**A Binance megmarad legitim történeti és kutatási adatforrásnak.**

Megőrizve és használható: a `data/candles` gyertyák, a
`freqtrade/user_data/data/binance/` historikus JSON-ok, a `replay_*.py`
eszközök, a `tools/fetch_historical_candles.py`, minden korábbi kísérleti
eredmény, manifest és reprodukálhatósági bizonyíték, valamint a
`BinanceAdapter.get_ticker` publikus ár-lekérdezés.

A `price_sources` alapértelmezett ár-forrása `binance` → `okx` lett — nem a
tiltás miatt, hanem mert egy OKX-en kereskedő bot Binance-alapértelmezésű
ára csendes bázis-eltérést okozna a döntési szintekben. A Binance explicit
kérésre (`exchange="binance"`) továbbra is elérhető.

## Mit NEM jelent

**A Binance kivezetése nem OKX élesítési engedély.**

```
OKX_LIVE_EXECUTION_AUTHORIZED = NO
```

A helyszín-kapu és az élesítési kapu szándékosan két külön, egymástól
független konstans (`ALLOWED_LIVE_EXECUTION_VENUES` és
`LIVE_EXECUTION_AUTHORIZED`). Az első most kinyílt az OKX-re; a második
**zárva marad**. Az OKX éles kereskedés külön tulajdonosi döntést és külön
tudományos outcome-engedélyt igényel, amely 2026-09-01-én nem létezik.

Ez a döntés nem érinti a futó publikus OKX kutatási adatgyűjtést
(`saturnus-v2-*` collectorok) — azok érintetlenül futnak tovább.

## Hogyan van kikényszerítve

A politika **kódban** él, nem konfigurációban — hogy egy visszaállított régi
config, egy elgépelt drop-in vagy egy félresikerült deploy ne tudja
visszakapcsolni:

| Réteg | Mechanizmus |
|---|---|
| Politika | `app/execution_venue_policy.py` – modul-konstansok, env-ből nem felülbírálhatók |
| Folyamat-indulás | `app/venue_preflight.py` systemd `ExecStartPre` a runneren **és** a Freqtrade-en, exit 78 |
| Restart-hurok elleni védelem | `Restart=on-failure` + `RestartPreventExitStatus=78` |
| Runner | `tick_runner.startup_venue_guard()` – a tick-hurok el sem indul |
| Order-út #1 | `freqtrade_executor` – FORCEENTER/FORCEEXIT elutasítva a kérés előtt |
| Order-út #2 | `freqtrade_adapter` – `/forcebuy`, `/forcesell` ugyanígy |
| Order-út #3 | `exchange/binance.py` – `create_order`/`cancel_order`/`convert_dust` `ExecutionVenueForbidden`-t dob, a capabilities nem deklarál rendelési támogatást |
| Order-út #4 | `app/liquidate_dust.py` – kivezetve (közvetlen aláírt Binance MARKET SELL volt) |
| Credential-út | `app/binance_wallet.py` – a kulcs beolvasása előtt rövidre zár |

A `LIVE_EXECUTION_AUTHORIZED` és az `ALLOWED_LIVE_EXECUTION_VENUES`
szándékosan **modul-konstans**, ugyanaz a minta, mint a stratégia
`SIGNAL_BASED_ENTRY_ENABLED`-jénél. Környezeti változóval nem kapcsolható
vissza; csak kódváltoztatással és külön review-val.

## Bizonyítás

78 viselkedési regressziós teszt (`tests/test_execution_venue_policy.py`),
és 8 production-oldali ellenőrzés a VPS-en. Egyik sem forrásszöveg-vizsgálat:
kivételtípust, verdikt-kódot, kilépési kódot mérnek, és bizonyítják, hogy egy
elutasított order **a hálózati réteget el sem éri**.

Részletek: `reports/uranus_audit_2026-09-01/URANUS_BINANCE_RETIREMENT_VERIFICATION_2026-09-01.md`.

## Nyitva maradt tulajdonosi teendő

A régi Binance API-kulcs **kompromittáltnak tekintendő** (a 2026-09-01-i audit
világ számára olvasható backupban találta meg). A kulcs tényleges visszavonása
csak a tulajdonos Binance-fiókjából végezhető el — a production fából már
eltávolítottuk, de az **a kulcsot magát nem érvényteleníti**.

A pontos lépéseket lásd a verifikációs jelentés „Tulajdonosi teendő" fejezetében.
