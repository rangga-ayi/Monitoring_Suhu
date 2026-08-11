# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Three-tier IoT temperature/humidity/pressure monitor with no backend of its own:

1. **Firmware** — Wemos D1 mini (ESP8266) + AHT20 & BMP280 over I2C, powered by 3× 18650 in parallel (1S BMS). Wakes from deep sleep every 5 min, pushes one record, sleeps again.
2. **Firebase Realtime Database** (`rangga-iot`, asia-southeast1) — the only shared state. All three tiers talk to each other exclusively through the RTDB schema below.
3. **Dashboard** — a single static `index.html` on GitHub Pages. Because Pages serves static files only, there is no server-side code anywhere; the dashboard is a pure RTDB reader.

The AI Insight feature exists *because* of that constraint: `analisa_lab.py` runs on the owner's Windows laptop (Task Scheduler, every 12 h) against a local Ollama instance, then writes the finished report back into the RTDB for the dashboard to render.

## Repository layout and what is actually deployed

| Path | Role | Tracked in git? |
|---|---|---|
| `index.html` | The entire dashboard — the only file GitHub Pages serves | yes |
| `Wemos_BMP280_ATH20_Firebase/*.ino` | Firmware sketch | no (ignored) |
| `analisa_lab.py` | AI report generator → `/LAPORAN_AI` | no (ignored) |
| `maintenance_db.py` | Full backup + prune of `/RIWAYAT_LAB` | no (ignored) |
| `rangga-iot-firebase-adminsdk-*.json` | Firebase service account for both scripts | no (`*.json`) |

**`.gitignore` gotcha:** the last line is `/*`, which ignores *everything* at the repo root. Only already-tracked files appear in `git status` — a new root-level file needs `git add -f` or it will silently never be committed. `origin/main` also still holds `Wemos_BMP280_ATH20_Firebase.ino` and `backup/index.html`/`backup/index1.html`, which are deleted locally but not yet committed as deletions.

**Secret already public:** the `.ino` committed at `origin/main` contains the live Firebase legacy database secret (`FIREBASE_AUTH`) in cleartext in a public repo. If it gets rotated, the firmware constant must be updated and reflashed. The WiFi credentials in that committed copy were partially sanitised; the local working copy has the real ones.

## Commands

No build step, no package manager, no tests, no linter in this repo.

**Dashboard** — must be served over HTTP (ES module imports from CDN; `file://` fails):

```bash
# XAMPP already serves the working directory
# http://localhost/Monitoring_Suhu/
python -m http.server 8000    # alternative
```

Deploy = commit `index.html` and push to `main`.

**Python scripts** (deps already installed: `firebase-admin`, `pandas`, `requests`, `ollama`):

```bash
python analisa_lab.py     # needs Ollama reachable at http://127.0.0.1:11434
python maintenance_db.py  # writes Backup_Firebase_*.json, then prompts y/n before deleting
```

**Firmware** — Arduino IDE (no `arduino-cli` installed). Board: LOLIN(WEMOS) D1 R2 & mini, ESP8266 core. Libraries: Adafruit AHTX0, Adafruit BMP280, Firebase ESP Client (mobizt). The `addons/TokenHelper.h` + `addons/RTDBHelper.h` includes are mandatory for legacy-token auth. Deep-sleep wake requires D0 wired to RST. Set `DEBUG_MODE = true` to stay awake and push every 10 s over serial instead of sleeping.

> ⚠️ **M10 (keselamatan):** JANGAN menancapkan USB saat baterai tersambung — pin `5V` D1 mini tersambung langsung ke VBUS (tanpa dioda), USB 5 V akan back-feed ke pack Li-ion tanpa kontrol arus/CV. **Cabut baterai dulu sebelum flash.** Solusi permanen: saklar pemutus baterai atau modul TP4056.

## RTDB data contract

`/RIWAYAT_LAB/<pushKey>` — one record per wake cycle:

```
suhu, kelembapan (AHT20) | tekanan (BMP280, MSLP) | tegangan_vcc | koneksi (SSID) | timestamp (server ms)
```

Fields are **conditionally omitted**: the firmware only writes `suhu`/`kelembapan` if `ahtStatus`, and `tekanan` if `bmpStatus`. Every consumer must treat a missing field as absent rather than zero — the dashboard guards with `!== undefined`, `analisa_lab.py` uses `pd.to_numeric(..., errors='coerce')`. `tegangan_vcc = 0.0` is a distinct sentinel meaning "ADC read failed" (`rawA0 < 50`), not a real reading.

`/LAPORAN_AI` — single node written with `set()` (overwrites, no history): `{timestamp, laporan_text}`. `laporan_text` is Markdown, rendered client-side by `marked`.

**Field opsional baru (sejak 2026-08-12, firmware FASE 1+3):** konsumen harus memperlakukan
ketidakhadirannya sebagai absen, bukan nol:

- `status_daya` — `"kritis"` (C2: cutoff < 3,40 V) / `"rendah"` / `"normal"`.
- `status_sensor` — `"semua_gagal"` saat kedua sensor error tapi daya tetap dikirim (M9).
- `rssi` — kekuatan sinyal WiFi (dBm), `adc_raw` — raw ADC A0 (memungkinkan rekalibrasi
  historis), `durasi_bangun_ms` — `millis()` tepat sebelum push.

## Constants coupled across tiers

Changing one of these requires changing its counterparts:

- **Cadence:** `SLEEP_SECONDS = 300` (firmware) ↔ 7-min offline threshold in `updateConnectionStatus()` (m1 — real interval is ~4.87 min plus WiFi gaps; 5.5 min caused 2.2 % false "Offline"; clock synced via `/.info/serverTimeOffset`, m2) ↔ `HARI_ANALISIS = 7` + `limit_to_last(HARI_ANALISIS * 288 * 2)` in `analisa_lab.py` (sliced by wall-clock time, not record-count-only) ↔ the "deep sleep 5 menit" claim in the AI prompt.
- **Battery:** `MULTIPLIER_BATERAI = 0.004038` (A0 voltage-divider calibration, tuned against a multimeter) ↔ `KURVA_BATERAI` piecewise percentage map + `peringatanBaterai` banner in the dashboard (M13 — replaced the old linear 3.2–4.2 V map) ↔ chart `y1` axis range 3.0–4.3 V ↔ the 0.00 V anomaly protocol in the AI prompt.
- **Pressure:** `ELEVASI_PERANGKAT = 8.0` m and `OFFSET_TEKANAN = 1.53` hPa mean the published `tekanan` is sea-level-corrected (MSLP), not the raw absolute reading.
- **Comfort bands** (temp 24/29/33 °C, humidity 40/65/80 %) are now driven by a single `AMBANG` object in `index.html` (m8), feeding gauge zones, color/status logic, and tooltip text.
- **Power protection (C2):** `BATAS_KRITIS = 3.40` V and `TIDUR_HEMAT_S = 3600` (firmware, hibernasi) ↔ `BATAS_PERINGATAN = 3.60` V → `status_daya` in RTDB ↔ `KURVA_BATERAI` piecewise map + `peringatanBaterai` banner (M13) ↔ `BATAS_BAWAH_VCC = 0.5` in `analisa_lab.py` (filters the 0.0 V ADC-fail sentinel).
- **Sensor sanity ranges** (`SUHU` −10/60, `HUM` 1/100, `TEK` 950/1050 hPa) are duplicated in firmware (M1) and both Python scripts (`RENTANG_WAJAR` in `analisa_lab.py` and `maintenance_db.py`). Editing a range means three edits.
- **WiFi fast-connect (M7):** `WiFi.persistent(false)`, `WiFi.setOutputPower(17.0)`, and `wifiMulti.run(20000)` single-call (no re-scan loop). IP config is NOT static — the 5 APs span different subnets, so DHCP is kept (ask the user before ever hardcoding an IP).
- **Calibration:** `MULTIPLIER_BATERAI = 0.004038` remains the 1-point factor until the user performs the 2-point calibration (`A_KALIBRASI`/`B_KALIBRASI`) after the M3 resistor swap (100 kΩ → 150 kΩ) and M4 100 nF cap. `adc_raw` is stored so historical re-calibration is possible without a reflash.

## Dashboard architecture (`index.html`, one `<script type="module">`)

Two mutually exclusive view modes share the same chart and table renderers, gated by `isLiveMode`:

- **Live** — `onChildAdded` on `query(dbRef, limitToLast(30))`; `unsubscribeLive` holds the detach function; rolling 30-point chart; `liveDataArray` kept newest-first.
- **Filter / history** — calls `unsubscribeLive()` to stop the realtime listener, then a one-shot `get()` into `filteredDataArray` with client-side pagination (`itemsPerPage = 30`).

Only live mode updates `lastDataTimestamp`/`lastSSID`, so the online/offline badge does not lie while browsing history. `getSortedData()` sorts the **table only** — the chart always renders chronologically, which is why `renderFilteredView()` slices `filteredDataArray` separately from the sorted copy.

The AI modal lazy-loads on `show.bs.modal` using `get()` (deliberately not `onValue`) and caches into `aiDataCached` — this is a read-quota decision, keep it that way.

Free-tier egress caution: "Export All" and `maintenance_db.py` each download the entire `/RIWAYAT_LAB` node (~700 KB as of the last backup and growing at 288 records/day).

The Tekanan card now has `#statusPress` and `#iconPress` (was m9) — the pressure status string and icon class computed in `updateCardsUI()` are surfaced instead of being silently discarded.

## Conventions

Everything is in Indonesian — identifiers, UI copy, comments, commit messages, and the AI prompt. Match that when adding code. The RTDB keys (`suhu`, `kelembapan`, `tekanan`, `tegangan_vcc`, `koneksi`) are the wire format between all three tiers *and* the schema of the stored history; renaming one orphans existing records.

`analisa_lab.py` fetches OpenWeatherMap, WAQI, and sunrise-sunset (keys hardcoded at the top) to give the model outdoor context for an indoor-vs-outdoor comparison. Each fetch is individually wrapped in `try/except` with neutral defaults so one dead API degrades the report instead of aborting it. `DAFTAR_MODEL_AI` is a top-down fallback chain — the script walks the list until a model responds, so cloud/large models can be listed first without risking a failed run.
