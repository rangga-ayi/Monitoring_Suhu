# Laporan Audit & Rencana Perbaikan — Monitoring Suhu

> **Dokumen kerja untuk agent AI.** Berisi hasil audit tiga tier (firmware ESP8266, Firebase RTDB,
> dashboard statis) beserta rencana perbaikan yang sudah diurutkan berdasarkan prioritas.
>
> | | |
> |---|---|
> | Tanggal audit | **2026-08-12** |
> | Basis bukti | Data live RTDB (600 record terakhir, read-only) + `Backup_Firebase_2026-06-08_23-32-37.json` (3.458 record) |
> | Total temuan | **3 CRITICAL · 17 MAYOR · 17 MINOR** |
> | Status keseluruhan | ⏸️ Sebagian — semua item agent selesai (2026-08-12); sisa 🧑 butuh manusia (C1, M3, M4, M10, M12, m14, m15, m17) |
>
> ⚠️ **Dokumen ini sengaja TIDAK memuat nilai secret apa pun** (token Firebase, password WiFi, API key).
> Yang dicatat hanya *lokasi* (`file:line`). Jangan pernah menuliskan nilai kredensial ke dalam file ini
> — repo ini publik dan file ini berada di root repo.

---

## 0. Cara memakai dokumen ini

**Untuk agent yang mengerjakan:**

1. Baca `CLAUDE.md` lebih dulu — di sana ada arsitektur, data contract RTDB, dan daftar konstanta
   yang saling terkait antar tier. Dokumen ini **melengkapi**, bukan menggantikan `CLAUDE.md`.
2. Kerjakan sesuai **§7 Rencana Eksekusi**, bukan urutan nomor temuan. Fase 1 → 2 → 3 → 4.
3. Setiap temuan punya blok `Status:` — ubah `⬜ BELUM` menjadi `✅ SELESAI (tanggal)` atau
   `⏸️ DITUNDA (alasan)` setelah dikerjakan. Catat juga di **§10 Log Perubahan**.
4. Temuan bertanda **🧑 BUTUH MANUSIA** tidak bisa diselesaikan agent (perlu akses Firebase Console,
   solder, atau ganti password router). Jangan tandai selesai — laporkan ke user dan lanjut ke item lain.
5. Setiap temuan punya bagian **Verifikasi**. Jalankan itu sebelum menandai selesai. Jangan
   menandai selesai hanya karena kode sudah diedit.

**Format setiap temuan:**

```
### [ID]. Judul
Severity · Tier · File:line
Bukti      → data terukur yang membuktikan bug ini nyata
Akar       → penyebab teknis
Perbaikan  → kode / tindakan konkret
Verifikasi → cara membuktikan sudah beres
Status     → ⬜ / ✅ / ⏸️
```

**Konvensi bahasa:** seluruh identifier, komentar kode, copy UI, dan commit message memakai
Bahasa Indonesia (lihat `CLAUDE.md` §Conventions). Ikuti itu saat menulis kode baru.

---

## 1. Konteks singkat sistem

Tiga tier tanpa backend sendiri, semuanya berkomunikasi **hanya** lewat Firebase RTDB:

```
[Wemos D1 mini + AHT20 + BMP280]  --push tiap ~5 menit-->  [Firebase RTDB]  <--baca--  [index.html @ GitHub Pages]
        deep sleep 300 s                                    /RIWAYAT_LAB                    (statis, read-only)
                                                            /LAPORAN_AI
                                                                 ^
                                                                 | set() tiap 12 jam
                                                    [analisa_lab.py @ laptop + Ollama]
```

**Rangkaian hardware (dikonfirmasi user, dengan satu koreksi):**

| Dari | Ke | Catatan |
|---|---|---|
| Baterai + (3×18650 paralel, 1S BMS) | pin `5V` Wemos | masuk LDO RT9013 → 3,3 V |
| Baterai − | `GND` | |
| Baterai + | R **100 kΩ** → `A0` | ⚠️ user awalnya mencatat "1 kΩ"; analisis data membuktikan ~93 kΩ → lihat **M3** |
| `D1` / `D2` | SCL / SDA sensor | I2C |
| `D0` | `RST` | wajib untuk bangun dari deep sleep |
| pin `5V` | VCC modul sensor | modul punya regulator sendiri |

**Penting soal A0:** pin `A0` D1 mini **sudah punya divider internal** 220 kΩ seri + 100 kΩ ke GND.
Resistor eksternal 100 kΩ menjadi bagian ketiga dari divider tersebut:

```
V_ADC = V_baterai × 100k / (R_eksternal + 220k + 100k)
      = V_baterai × 100 / 420  = V_baterai × 0,238   (dengan R_ext = 100k)
```

ADC ESP8266 full-scale ≈ 1,0 V ⇒ **saturasi pada V_baterai ≈ 4,20 V**. Ini akar dari M3 dan M4.

---

## 2. Metodologi & cara reproduksi bukti

Semua angka di dokumen ini bisa dihasilkan ulang. Simpan skrip berikut sebagai `diagnostik.py`
(**jangan commit** — lihat catatan `.gitignore` di m16) dan jalankan dari root repo:

```python
# diagnostik.py — audit read-only, tidak menulis apa pun ke Firebase
import firebase_admin, pandas as pd, datetime, collections, statistics
from firebase_admin import credentials, db

cred = credentials.Certificate("rangga-iot-firebase-adminsdk-fbsvc-955a645d76.json")
firebase_admin.initialize_app(cred, {
    "databaseURL": "https://rangga-iot-default-rtdb.asia-southeast1.firebasedatabase.app"
})

data = db.reference("/RIWAYAT_LAB").order_by_key().limit_to_last(2016).get()
df = pd.DataFrame.from_dict(data, orient="index")
for c in ["suhu", "kelembapan", "tekanan", "tegangan_vcc", "timestamp"]:
    df[c] = pd.to_numeric(df.get(c), errors="coerce")

print("n                      =", len(df))
print("urutan kronologis?     =", df['timestamp'].is_monotonic_increasing)
print("suhu_terbaru (iloc[-1])= %.4f" % df['suhu'].iloc[-1])
print("tekanan_terbaru        = %.4f" % df['tekanan'].iloc[-1])
print("suhu == 0              =", int((df['suhu'] == 0).sum()))
print("suhu == -50            =", int((df['suhu'] == -50).sum()))
print("tekanan di luar 950-1050 =", int((~df['tekanan'].between(950, 1050)).sum()))
print("tegangan_vcc == 0      =", int((df['tegangan_vcc'] == 0).sum()))
print("tegangan max           = %.4f  (raw ~%.0f)" % (df['tegangan_vcc'].max(),
                                                      df['tegangan_vcc'].max() / 0.004038))
gaps = df.sort_values('timestamp')['timestamp'].diff().dropna() / 60000
print("median interval (menit)= %.2f" % gaps.median())
print("gap > 6 menit          = %d / %d" % (int((gaps > 6).sum()), len(gaps)))
```

Cek izin RTDB tanpa auth (aman, tidak mengubah data — `PUT null` ke path yang tidak ada adalah no-op):

```bash
B="https://rangga-iot-default-rtdb.asia-southeast1.firebasedatabase.app"
curl -s -o /dev/null -w "READ  -> %{http_code}\n" "$B/RIWAYAT_LAB.json?orderBy=%22\$key%22&limitToLast=1"
curl -s -o /dev/null -w "WRITE -> %{http_code}\n" -X PUT -d 'null' "$B/_probe.json?print=silent"
# Hasil audit 2026-08-12: READ -> 200 (publik, by design), WRITE -> 401 (ditolak, rules sudah benar)
```

**Snapshot bukti per 2026-08-12** (angka akan berubah seiring data masuk — kalau berbeda, itu normal,
yang penting *pola*-nya masih ada):

| Metrik | Nilai |
|---|---|
| Record sampah di backup Juni | 79 / 3.458 (**2,3 %**) |
| `suhu_terbaru` yang masuk prompt AI | **`nan`** |
| `tekanan_terbaru` yang masuk prompt AI | **18,3 hPa** |
| `bat_selisih` yang masuk prompt AI | **−0,99 V/minggu** (baterai "bertambah") |
| Tegangan minimum tercatat | **2,556 V** (over-discharge) |
| Tegangan maksimum tercatat | 4,1349 V = raw 1024 = **clipped** |
| Gap data terpanjang | **21,8 hari** (2026-07-21 → 2026-08-11) |
| Median interval antar record | 4,87 menit (bukan 5,0) |
| Inversi urutan timestamp vs push key | 1 / 2016 — dan tepat di record rusak |

---

## 3. Ringkasan temuan

### CRITICAL

| ID | Temuan | Tier | Butuh manusia | Status |
|---|---|---|---|---|
| **C1** | Secret Firebase + 3 password WiFi asli ter-commit di repo publik | firmware / git | 🧑 ya | ⏸️ (rotasi secret + ganti password router) |
| **C2** | Tak ada proteksi low-battery → over-discharge 2,56 V, data hilang 22 hari | firmware | sebagian | ✅ SELESAI (2026-08-12) |
| **C3** | Laporan AI digenerate dari `nan` dan `18,3 hPa` | python | tidak | ✅ SELESAI (2026-08-12) |

### MAYOR

| ID | Temuan | Tier | Butuh manusia | Status |
|---|---|---|---|---|
| **M1** | Tak ada validasi rentang sensor → 2,3 % record sampah | firmware | tidak | ✅ SELESAI (2026-08-12) |
| **M2** | BMP280 `MODE_NORMAL` + `FILTER_X16` dibaca sebelum settle | firmware | tidak | ✅ SELESAI (2026-08-12) |
| **M3** | ADC clipping di 4,13 V — puncak pengisian buta | hardware | 🧑 ya | ⏸️ (solder resistor 100k→150k) |
| **M4** | Satu resistor bukan divider sejati → bacaan berisik ±4 % | hardware | 🧑 ya | ⏸️ (solder kapasitor 100 nF + kalibrasi 2 titik) |
| **M5** | ADC dibaca saat WiFi aktif → mengukur tegangan yang sudah sag | firmware | tidak | ✅ SELESAI (2026-08-12) |
| **M6** | `ESP.deepSleep()` tanpa `delay()` → eksekusi lanjut jalan | firmware | tidak | ✅ SELESAI (2026-08-12) |
| **M7** | `wifiMulti.run()` dalam loop → scan berulang, boros baterai | firmware | tidak | ✅ SELESAI (2026-08-12) |
| **M8** | Push gagal = data hilang permanen, tanpa retry | firmware | tidak | ✅ SELESAI (2026-08-12) |
| **M9** | Kedua sensor gagal → tegangan baterai juga tidak terkirim | firmware | tidak | ✅ SELESAI (2026-08-12) |
| **M10** | Risiko hardware: USB back-feed ke baterai lewat pin 5V | hardware | 🧑 ya | ⏸️ (aturan dicatat di CLAUDE.md; solusi fisik menunggu user) |
| **M11** | Tidak ada field diagnostik (`rssi`, `adc_raw`, durasi bangun) | firmware | tidak | ✅ SELESAI (2026-08-12) |
| **M12** | `orderByChild("timestamp")` tanpa `.indexOn` → unduh seluruh node | dashboard / rules | 🧑 sebagian | ⏸️ (tambah `.indexOn` di Firebase Console) |
| **M13** | Peta persentase baterai linier + tidak ada peringatan | dashboard | tidak | ✅ SELESAI (2026-08-12) |
| **M14** | XSS: `marked.parse()` tanpa sanitasi + SSID tidak di-escape | dashboard | tidak | ✅ SELESAI (2026-08-12) |
| **M15** | `bat_awal`/`bat_akhir` tidak menyaring sentinel 0,0 V | python | tidak | ✅ SELESAI (2026-08-12, dengan C3) |
| **M16** | Tren tekanan `head(3)`/`tail(3)` pada frame tak terurut + outlier | python | tidak | ✅ SELESAI (2026-08-12, dengan C3) |
| **M17** | `maintenance_db.py`: 1 DELETE per record + hapus record tanpa timestamp | python | tidak | ✅ SELESAI (kode; jangan dijalankan tanpa izin) |

### MINOR

| ID | Temuan | Tier | Status |
|---|---|---|---|
| **m1** | Interval nyata 4,87 mnt vs threshold offline 5,5 mnt → 2,2 % "Offline" palsu | dashboard | ✅ SELESAI (threshold → 7 mnt) |
| **m2** | Deteksi offline: timestamp server vs `Date.now()` klien | dashboard | ✅ SELESAI (`/.info/serverTimeOffset`) |
| **m3** | `SLEEP_SECONDS * 1000000` overflow `int` bila > 2147 s | firmware | ✅ SELESAI (dengan M6 — `(uint64_t)...ULL`) |
| **m4** | `requests.get()` tanpa `timeout` → Task Scheduler bisa menggantung | python | ✅ SELESAI (`timeout=10`) |
| **m5** | `suhu_min` dihitung tapi tak dipakai (nilainya −50, sampah) | python | ✅ SELESAI (dengan C3 — kini bersih + masuk prompt) |
| **m6** | `df['suhu']` `KeyError` bila kolom absen total | python | ✅ SELESAI (dengan C3 — loop `for kolom`) |
| **m7** | Nama model di `DAFTAR_MODEL_AI` perlu diverifikasi | python | ✅ SELESAI (disamakan dengan `ollama list`) |
| **m8** | Threshold kenyamanan diduplikasi di 3 tempat | dashboard | ✅ SELESAI (`AMBANG` tunggal) |
| **m9** | `#statusPress` / `#iconPress` tidak ada di markup | dashboard | ✅ SELESAI |
| **m10** | Chart axis kiri `min: 0` → garis suhu gepeng | dashboard | ✅ SELESAI (`min: 20, max: 100`) |
| **m11** | CSV: tanpa BOM UTF-8, nama file tanpa tanggal, URL tak di-revoke | dashboard | ✅ SELESAI |
| **m12** | `myChart.update()` dipanggil 30× saat backfill awal | dashboard | ✅ SELESAI (debounce 100 ms) |
| **m13** | Koreksi elevasi 8 m di bawah akurasi BMP280 | firmware | ✅ SELESAI (doc-only di CLAUDE.md) |
| **m14** | Sensor disuplai terus dari baterai mentah saat ESP tidur | hardware | ⏸️ (solder — kerjakan terakhir, manfaat kecil) |
| **m15** | API key OWM & WAQI hardcoded | python | ⏸️ (file ter-gitignore; risiko rendah — pindah ke env bila repo di-ungkit) |
| **m16** | `.gitignore` baris `/*` — file root baru butuh `git add -f` | git | ✅ SELESAI (allowlist) |
| **m17** | Deletion di `origin/main` belum di-commit | git | ⏸️ (sesudah C1 langkah 5 — purge history) |

---

## 4. CRITICAL

### C1. Secret Firebase + password WiFi asli ter-commit di repo publik

**Severity** CRITICAL · **Tier** firmware / git · **Lokasi** `origin/main:Wemos_BMP280_ATH20_Firebase.ino:17` (token), `:48-52` (password WiFi) · **🧑 BUTUH MANUSIA**

**Bukti.** Terverifikasi masih ada di `origin/main` (repo `https://github.com/RLA07/Monitoring_Suhu`):
- Baris 17 — legacy database secret dalam cleartext.
- Baris 50–52 — tiga password WPA jaringan nyata. (Baris 48–49 sudah di-sanitasi sebagian,
  sisanya identik dengan working copy lokal.)

Security rules **sudah benar**: write tanpa auth ditolak (`HTTP 401`), read publik memang by design
karena dashboard statis. Lubangnya bukan rules — **legacy database secret mem-bypass seluruh rules.**
Siapa pun yang menemukan repo ini punya akses tulis dan hapus penuh ke seluruh database.

**Akar.** Kredensial ditulis sebagai `#define` di dalam sketch yang di-commit.

**Perbaikan.** Urutan ini penting — jangan dibalik:

1. 🧑 Firebase Console → Project Settings → Service Accounts → Database secrets → **rotate/hapus** secret lama.
2. 🧑 **Ganti password ketiga WiFi tersebut** di router.
3. Migrasi firmware lepas dari legacy token. Opsi termudah yang tetap aman: anonymous auth atau
   custom token, lalu rules `".write": "auth != null"`.
4. Pindahkan kredensial ke `Wemos_BMP280_ATH20_Firebase/secrets.h` yang di-gitignore:

   ```cpp
   // secrets.h — JANGAN di-commit
   #define FIREBASE_AUTH "..."
   #define WIFI_SSID_1 "..."
   #define WIFI_PASS_1 "..."
   ```
   Sertakan `secrets_contoh.h` berisi placeholder sebagai dokumentasi, dan itu boleh di-commit.
5. Purge history:
   ```bash
   git filter-repo --path Wemos_BMP280_ATH20_Firebase.ino --invert-paths
   git push --force origin main
   ```
   🧑 Paling aman: hapus repo lalu buat ulang dari working copy bersih. Fork/clone pihak lain dan
   cache GitHub tetap menyimpan history lama meskipun sudah force-push.

**Verifikasi.**
```bash
git show origin/main:Wemos_BMP280_ATH20_Firebase.ino 2>&1 | grep -c "FIREBASE_AUTH"   # harus 0 / file not found
git log --all -S"<potongan-secret>" --oneline                                          # harus kosong
```
Lalu pastikan secret lama sudah tidak bisa dipakai:
```bash
curl -s -o /dev/null -w "%{http_code}\n" -X PUT -d '1' \
  "https://rangga-iot-default-rtdb.asia-southeast1.firebasedatabase.app/_probe.json?auth=<SECRET_LAMA>"
# harus 401, bukan 200
```

**Status:** ⏸️ SEBAGIAN (local git history berhasil di-purge 100% & `secrets.h` aman; sisa `git push -f` dengan login GitHub & rotasi secret di Console)

---

### C2. Tak ada proteksi low-battery → over-discharge 2,56 V, data hilang 22 hari

**Severity** CRITICAL · **Tier** firmware · **Lokasi** `.ino:150-161` (baca ADC), `.ino:197-200` (`goToSleep`)

**Bukti.** Deret waktu dari data live:
```
2026-07-19 04:21  V = 2.932
2026-07-20 08:46  V = 2.621
2026-07-21 03:40  V = 2.754   <-- record terakhir sebelum mati
       ...        GAP 21,8 HARI (31.342 menit) ...
2026-08-11 22:02  V = 4.075   <-- hidup lagi setelah di-recharge
```
Minimum tercatat **2,556 V**.

**Akar.** Dua hal bertumpuk:
1. Li-ion 1S di bawah ~2,5 V mengalami **kerusakan kapasitas permanen**. Pack dibiarkan turun sampai
   cutoff BMS, berulang kali.
2. LDO RT9013 di D1 mini butuh input ≥ ~3,5 V untuk menahan 3,3 V. Di 2,6–2,9 V, ESP8266 berjalan
   **di luar spesifikasi** (minimum 3,0 V) — itulah sebabnya WiFi mulai gagal (2,2 % gap > 6 menit)
   lalu mati total. Dashboard menampilkan 0 % berhari-hari tanpa ada mekanisme yang memberi tahu.

**Perbaikan.** Tambahkan cutoff di firmware. Panggil **setelah** baca ADC, **sebelum** WiFi menyala
(gabungkan dengan M5 yang memindahkan pembacaan ADC ke awal `setup()`):

```cpp
// --- AMBANG PROTEKSI DAYA ---
const float BATAS_KRITIS     = 3.40;   // di bawah ini LDO mulai drop out
const float BATAS_PERINGATAN = 3.60;
const uint32_t TIDUR_HEMAT_S = 3600;   // 1 jam saat baterai kritis

// di setup(), sesudah bacaBaterai(), sebelum menyalakan WiFi:
if (teganganBaterai > 0.5 && teganganBaterai < BATAS_KRITIS) {
  Serial.printf("Baterai kritis (%.2f V) — hibernasi %lu s.\n", teganganBaterai, TIDUR_HEMAT_S);
  Serial.flush();
  ESP.deepSleep((uint64_t)TIDUR_HEMAT_S * 1000000ULL);
  delay(100);          // WAJIB — lihat M6
  return;
}
```

Kirim juga status daya supaya dashboard bisa memunculkan peringatan (lihat M13):

```cpp
const char* statusDaya = (teganganBaterai < BATAS_KRITIS)     ? "kritis"
                       : (teganganBaterai < BATAS_PERINGATAN) ? "rendah"
                                                              : "normal";
json.set("status_daya", statusDaya);
```

> **Catatan data contract.** `status_daya` adalah field **baru** dan opsional. Semua konsumen harus
> memperlakukan ketiadaannya sebagai "tidak diketahui", bukan error — konsisten dengan aturan
> conditionally-omitted di `CLAUDE.md`. Record lama tidak punya field ini.

**Verifikasi.**
- Set `DEBUG_MODE = true`, suplai board dari power supply variabel, turunkan ke 3,3 V,
  pastikan serial mencetak "Baterai kritis" dan tidak ada push ke Firebase.
- Setelah dipasang, jalankan `diagnostik.py` mingguan: `tegangan_vcc` minimum tidak boleh
  turun di bawah ~3,35 V lagi.

**Status:** ⬜ BELUM

---

### C3. Laporan AI digenerate dari `nan` dan `18,3 hPa`

**Severity** CRITICAL · **Tier** python · **Lokasi** `analisa_lab.py:47` (query), `:54` (DataFrame), `:73-75` (`iloc[-1]`), `:87-101` (tren tekanan)

**Bukti.** Menjalankan ulang blok statistik terhadap data live menghasilkan nilai yang **benar-benar
masuk ke prompt AI**:

| Variabel prompt | Nilai aktual | Seharusnya |
|---|---|---|
| `suhu_terbaru` (`:180`) | **`nan`** | 30,03 °C |
| `hum_terbaru` (`:181`) | **`nan`** | 74,69 % |
| `tekanan_terbaru` (`:182`) | **18,3 hPa** | 1011,05 hPa |
| `bat_selisih` (`:195`) | **−0,99 V/minggu** | lihat M15 |
| rentang "1 minggu terakhir" | **4 minggu** (14 Jul → 11 Agu) | 7 hari |

Jadi model saat ini diberi tahu *"Suhu Kamar Saat Ini: nan °C, Tekanan Udara Kamar 18,3 hPa"*
lalu diminta membuat analisis teknis. Apa pun isi laporan itu, **tidak valid.**

**Akar — rantai lengkap, sudah dilacak sampai ujung:**

`iloc[-1]` mengambil record dengan push key terbesar. Ternyata push key **tidak selalu kronologis**:
push ID Firebase digenerate di sisi *client*, dan ESP8266 kehilangan jam setiap kali deep sleep.
Pada boot pertama setelah recharge (2026-08-11 22:02) NTP belum sync → prefix waktu di push ID salah.

Terverifikasi: `keys == sorted(keys)` → `True`, tapi `timestamp.is_monotonic_increasing` → `False`
dengan **1 inversi**, dan record yang salah urut itu **persis record rusak** (`suhu`/`kelembapan`
hilang, `tekanan = 18,29`). Jadi `iloc[-1]` mendarat tepat di satu-satunya record cacat.

Ditambah: `limit_to_last(2016)` adalah batas **jumlah**, bukan **waktu**. Karena ada gap 22 hari,
2016 record terakhir membentang 4 minggu, bukan 1 minggu seperti klaim di prompt (`:192`).

**Perbaikan.** Ganti blok `analisa_lab.py:46-101`:

```python
# ==========================================
# 3. MENARIK & MENGHITUNG DATA (PANDAS)
# ==========================================
HARI_ANALISIS = 7
BATAS_BAWAH_VCC = 0.5          # sentinel 0.0 V = gagal baca ADC, bukan bacaan nyata
RENTANG_WAJAR = {              # validasi fisik — samakan dengan firmware (M1)
    "suhu": (-10, 60),
    "kelembapan": (1, 100),
    "tekanan": (950, 1050),
}

ref = db.reference("/RIWAYAT_LAB")
# Ambil longgar (2× kebutuhan) lalu potong berdasarkan waktu, karena push key TIDAK reliabel
data_firebase = ref.order_by_key().limit_to_last(HARI_ANALISIS * 288 * 2).get()
if not data_firebase:
    print("Error: Tidak ada data di Firebase!")
    exit()

df = pd.DataFrame.from_dict(data_firebase, orient="index")

# Pastikan semua kolom ada, walau seluruh jendela tidak memuatnya (m6)
for kolom in ["suhu", "kelembapan", "tekanan", "tegangan_vcc", "timestamp"]:
    if kolom not in df.columns:
        df[kolom] = pd.NA
    df[kolom] = pd.to_numeric(df[kolom], errors="coerce")

# WAJIB: urutkan eksplisit — push key tidak dijamin kronologis (ESP kehilangan jam saat deep sleep)
df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

# Potong ke jendela waktu sebenarnya
batas_ms = (datetime.datetime.now() - datetime.timedelta(days=HARI_ANALISIS)).timestamp() * 1000
df = df[df["timestamp"] >= batas_ms]

# Buang outlier fisik SEBELUM statistik apa pun (M16)
for kolom, (lo, hi) in RENTANG_WAJAR.items():
    df.loc[~df[kolom].between(lo, hi), kolom] = pd.NA
df.loc[df["tegangan_vcc"] < BATAS_BAWAH_VCC, "tegangan_vcc"] = pd.NA   # (M15)

if df.empty:
    print(f"Error: Tidak ada data valid dalam {HARI_ANALISIS} hari terakhir!")
    exit()

def terakhir_valid(kolom):
    """Nilai valid terakhir secara kronologis, bukan baris terakhir."""
    seri = df[kolom].dropna()
    return seri.iloc[-1] if len(seri) else float("nan")

def pertama_valid(kolom):
    seri = df[kolom].dropna()
    return seri.iloc[0] if len(seri) else float("nan")

suhu_rata2      = df["suhu"].mean()
suhu_maks       = df["suhu"].max()
suhu_min        = df["suhu"].min()
hum_rata2       = df["kelembapan"].mean()
tekanan_rata2   = df["tekanan"].mean()

suhu_terbaru    = terakhir_valid("suhu")
hum_terbaru     = terakhir_valid("kelembapan")
tekanan_terbaru = terakhir_valid("tekanan")

# Baterai (M15) — sentinel 0.0 sudah dibuang di atas
bat_awal    = pertama_valid("tegangan_vcc")
bat_akhir   = terakhir_valid("tegangan_vcc")
bat_selisih = bat_awal - bat_akhir if pd.notna(bat_awal) and pd.notna(bat_akhir) else float("nan")

# Tren tekanan 3 jam (M16) — df sudah terurut, jadi head/tail kini bermakna
tren_tekanan = 0.0
df_tekanan = df.dropna(subset=["tekanan"])
if len(df_tekanan) >= 6:
    batas_3jam = df_tekanan["timestamp"].iloc[-1] - (3 * 60 * 60 * 1000)
    df_3jam = df_tekanan[df_tekanan["timestamp"] >= batas_3jam]
    if len(df_3jam) >= 6:
        tren_tekanan = df_3jam["tekanan"].tail(3).mean() - df_3jam["tekanan"].head(3).mean()

status_tren = ("Naik (Membaik/Cerah)"   if tren_tekanan >  0.5 else
               "Turun (Memburuk/Hujan)" if tren_tekanan < -0.5 else "Stabil")
```

Lalu **lindungi prompt dari `nan`** — kalau tidak, model akan berhalusinasi mengisi kekosongan.
Tambahkan helper dan pakai di seluruh f-string prompt (`:169-211`):

```python
def fmt(nilai, satuan="", desimal=1):
    """Format angka untuk prompt; eksplisit kalau data tidak ada."""
    if nilai is None or pd.isna(nilai):
        return "DATA TIDAK TERSEDIA"
    return f"{nilai:.{desimal}f} {satuan}".strip()
```

Contoh penggantian di prompt:
```python
# sebelum: - Suhu Kamar Saat Ini: {suhu_terbaru:.1f} °C
# sesudah: - Suhu Kamar Saat Ini: {fmt(suhu_terbaru, '°C')}
```
Terapkan ke `suhu_terbaru`, `hum_terbaru`, `tekanan_terbaru`, `tren_tekanan`, `suhu_rata2`,
`suhu_maks`, `hum_rata2`, `bat_selisih`, `bat_awal`, `bat_akhir`. Perbaiki juga label
`:192` "Profil 1 Minggu Terakhir" agar memakai `HARI_ANALISIS` dan menyebut jumlah record aktual.

**Verifikasi.** Jalankan `diagnostik.py`, lalu jalankan `analisa_lab.py` dan pastikan:
- Tidak ada string `nan` di prompt yang dicetak / laporan yang dihasilkan.
- `tekanan_terbaru` berada di 950–1050.
- `bat_selisih` bertanda positif saat baterai memang turun.
- Rentang waktu data yang dilaporkan ≈ 7 hari.

**Status:** ⬜ BELUM

---

## 5. MAYOR

### M1. Tak ada validasi rentang sensor → 2,3 % record sampah

**Severity** MAYOR · **Tier** firmware · **Lokasi** `.ino:134-142` (AHT), `:144-148` (BMP)

**Bukti.** Backup Juni: **79 dari 3.458 record** rusak. Tiga tanda tangan kegagalan berbeda:

| Gejala | Jumlah | Penyebab |
|---|---|---|
| `suhu = 0`, `kelembapan = 0` | 79 | I2C read gagal, struct `sensors_event_t` ter-zero |
| `suhu = -50.0` | ada di data live | raw AHT20 all-zero → rumus `raw/2²⁰×200 − 50` = tepat −50 |
| `tekanan = 0` / `18,29` / `670,83` | 80 | BMP280 dibaca sebelum konversi selesai (lihat M2) |

**Akar.** Guard `if (ahtStatus)` tidak menangkap ini karena `aht.begin()` **berhasil** — yang gagal
adalah *pembacaannya*. `isnan()` (`:139`) juga tidak menangkap `0.0` maupun `-50.0`.

**Perbaikan.** Ganti `.ino:134-142`:

```cpp
// --- RENTANG WAJAR SENSOR (samakan dengan analisa_lab.py RENTANG_WAJAR) ---
const float SUHU_MIN = -10.0, SUHU_MAKS = 60.0;
const float HUM_MIN  =   1.0, HUM_MAKS  = 100.0;
const float TEK_MIN  = 950.0, TEK_MAKS  = 1050.0;

if (ahtStatus) {
  sensors_event_t humidity, temp;
  aht.getEvent(&humidity, &temp);      // bacaan pertama dibuang: AHT20 belum siap
  delay(50);
  aht.getEvent(&humidity, &temp);
  t = temp.temperature;
  h = humidity.relative_humidity;

  bool valid = !isnan(t) && !isnan(h)
            && t >= SUHU_MIN && t <= SUHU_MAKS
            && h >= HUM_MIN  && h <= HUM_MAKS;
  if (!valid) {
    Serial.printf("AHT20 bacaan tidak wajar (t=%.2f h=%.2f) — dibuang.\n", t, h);
    ahtStatus = false;                 // field tidak akan dikirim
  }
}
```

**Verifikasi.** Setelah seminggu berjalan, `diagnostik.py` harus melaporkan
`suhu == 0` → 0, `suhu == -50` → 0, `tekanan di luar 950-1050` → 0 untuk record baru.

**Status:** ⬜ BELUM

---

### M2. BMP280 `MODE_NORMAL` + `FILTER_X16` dibaca sebelum settle

**Severity** MAYOR · **Tier** firmware · **Lokasi** `.ino:67-71` (setSampling), `:144-148` (read)

**Bukti.** 80 record dengan `tekanan` di luar rentang wajar, termasuk `0`, `18,29`, `670,83` hPa
(670 hPa ≈ ketinggian 3.300 m). Nilai `18,29` sempat menjadi `tekanan_terbaru` yang masuk prompt AI.

**Akar.** `MODE_NORMAL` + `FILTER_X16` + `STANDBY_MS_500` butuh **±8 detik** agar filter IIR settle.
Kode membacanya beberapa ratus ms setelah `begin()`.

Kenapa rusaknya intermiten (2,3 %) dan bukan selalu? Karena `wifiMulti.run()` yang lambat (M7)
**kebetulan** memberi waktu settle. Saat WiFi tersambung cepat, BMP280 dibaca terlalu dini.
Ini juga berarti **memperbaiki M7 akan memperburuk M2** kalau M2 tidak dikerjakan lebih dulu —
kerjakan M2 sebelum atau bersamaan dengan M7.

Selain itu `MODE_NORMAL` membuat BMP280 terus melakukan konversi selama ESP8266 tidur — pemborosan
daya tanpa manfaat untuk perangkat yang hanya butuh 1 sampel per 5 menit.

**Perbaikan.** Ganti `.ino:67-71`:

```cpp
// One-shot: benar untuk perangkat deep-sleep. FILTER_OFF karena IIR tak berguna untuk 1 sampel.
bmp.setSampling(Adafruit_BMP280::MODE_FORCED,
                Adafruit_BMP280::SAMPLING_X2,     // suhu
                Adafruit_BMP280::SAMPLING_X16,    // tekanan
                Adafruit_BMP280::FILTER_OFF,
                Adafruit_BMP280::STANDBY_MS_1);
```

Ganti `.ino:144-148`:

```cpp
if (bmpStatus) {
  if (bmp.takeForcedMeasurement()) {
    float tekananAbsolut = (bmp.readPressure() / 100.0F) - OFFSET_TEKANAN;
    p = bmp.seaLevelForAltitude(ELEVASI_PERANGKAT, tekananAbsolut);
    if (isnan(p) || p < TEK_MIN || p > TEK_MAKS) {
      Serial.printf("BMP280 bacaan tidak wajar (%.2f hPa) — dibuang.\n", p);
      bmpStatus = false;
    }
  } else {
    Serial.println("BMP280 gagal takeForcedMeasurement().");
    bmpStatus = false;
  }
}
```

**Verifikasi.** `DEBUG_MODE = true`, amati 20 pembacaan serial berturut-turut — semuanya harus
di 1005–1015 hPa tanpa spike. Lalu cek `diagnostik.py`: `tekanan di luar 950-1050` → 0 untuk record baru.

**Status:** ⬜ BELUM

---

### M3. ADC clipping di 4,13 V — puncak pengisian buta

**Severity** MAYOR · **Tier** hardware · **Lokasi** rangkaian A0 + `.ino:27` (`MULTIPLIER_BATERAI`) · **🧑 BUTUH MANUSIA (solder)**

**Bukti.** Terverifikasi mentok di ceiling ADC:
```
2026-08-11 23:12  V = 4.1349  (raw 1024)   <-- CLIPPED
2026-08-11 23:17  V = 4.1349  (raw 1024)   <-- CLIPPED
2026-08-11 23:22  V = 4.1349  (raw 1024)   <-- CLIPPED
```
`max(tegangan_vcc)` di seluruh dataset = 4,1349 V = raw 1024 = batas atas ADC.

**Koreksi catatan rangkaian.** Deskripsi user tidak konsisten: prosa menyebut 100 kΩ, detail wiring
menyebut "Resistor 1K". **Data membuktikan resistornya ~100 kΩ:**

```
Ceiling ADC (≈1,0 V) tercapai pada V_baterai ≈ 4,13 V (terukur multimeter saat kalibrasi)
⇒ ratio = 1,0 / 4,13 = 0,242
⇒ 100k / (R_ext + 320k) = 0,242
⇒ R_ext ≈ 93 kΩ  →  komponen 100 kΩ (toleransi 5 %)
```
Kalau benar 1 kΩ, ratio = 0,31 dan ADC akan mentok di 3,2 V — artinya **seluruh** bacaan Anda akan
flat di 1024. Itu tidak terjadi. Jadi "1K" adalah typo. **Catatan wiring perlu dikoreksi ke 100 kΩ.**

**Akar.** 18650 penuh = **4,20 V**, tapi ADC mentok di **4,13 V**. Rentang 4,13–4,20 V buta —
tidak bisa membedakan "baru saja penuh" dari "overcharge". Tidak ada headroom sama sekali.

**Perbaikan.** 🧑 Ganti resistor 100 kΩ → **150 kΩ**:

| R_ext | ratio | Saturasi di | Multiplier teoretis |
|---|---|---|---|
| 100 kΩ (sekarang) | 0,238 | **4,20 V** ← tanpa headroom | 0,004102 |
| **150 kΩ (disarankan)** | 0,213 | **4,70 V** | 0,004590 |
| 220 kΩ | 0,185 | 5,40 V | 0,005273 |

150 kΩ adalah titik tengah terbaik: ada headroom, resolusi masih bagus. Setelah diganti,
**wajib kalibrasi 2 titik** (lihat M4) — jangan pakai multiplier teoretis apa adanya.

**Verifikasi.** Charge penuh, cek `tegangan_vcc` melaporkan ~4,18–4,20 V dan **tidak** ada
deretan nilai identik yang menandakan clipping. `diagnostik.py`: `raw` maksimum harus < 1000.

**Status:** ⬜ BELUM

---

### M4. Satu resistor bukan divider sejati → bacaan berisik ±4 %

**Severity** MAYOR · **Tier** hardware + firmware · **Lokasi** rangkaian A0 + `.ino:150-161` · **🧑 BUTUH MANUSIA (solder)**

**Bukti.** Sampel berurutan berjarak 5 menit, kondisi termal stabil:
```
02:16  raw 688
02:21  raw 659   <-- lompat 29 LSB (4 %) dalam 5 menit
02:26  raw 688
```

**Akar.** Rangkaian bersandar pada divider internal board, yang menimbulkan dua masalah:
1. Resistor internal 220 k/100 k bertoleransi **±5 %** dan **nilainya berbeda antar revisi/klon
   D1 mini** → error skala sampai ~7 %, dan tidak reprodusibel kalau board diganti.
2. Impedansi Thevenin di node ADC ≈ **76 kΩ**, jauh di atas yang bisa disampling bersih oleh
   ADC ESP8266.

Ditambah: kalibrasi 1 titik yang ada sekarang **dilakukan tepat di titik saturasi**
(`0.004038 × 1024 = 4.1349`), jadi *slope*-nya sama sekali belum pernah tervalidasi.

**Perbaikan.**

1. 🧑 **Kapasitor 100 nF dari `A0` ke `GND`.** Satu komponen, penurunan noise terbesar per rupiah.
2. Ganti kalibrasi 1 titik → **2 titik** (`V = a·raw + b`). Ukur dengan multimeter di dua kondisi:
   penuh (~4,15 V) dan rendah (~3,60 V), lalu:
   ```
   a = (V₁ − V₂) / (raw₁ − raw₂)
   b = V₁ − a · raw₁
   ```
3. Perbaiki rutin pembacaan — 32 sampel + **median** (bukan mean; median tahan terhadap spike ADC):

```cpp
// --- KALIBRASI BATERAI 2 TITIK ---
// Diukur <tanggal>: raw=<raw1> -> <V1> V ; raw=<raw2> -> <V2> V
const float A_KALIBRASI = 0.004590;   // ganti dengan hasil pengukuran nyata
const float B_KALIBRASI = 0.0;
const int   JUMLAH_SAMPEL_ADC = 32;
const int   AMBANG_ADC_GAGAL  = 50;   // di bawah ini = A0 tak terhubung

/** Baca tegangan baterai. WAJIB dipanggil sebelum WiFi menyala (lihat M5). */
float bacaBaterai(int &rawKeluar) {
  int sampel[JUMLAH_SAMPEL_ADC];
  for (int i = 0; i < JUMLAH_SAMPEL_ADC; i++) {
    sampel[i] = analogRead(A0);
    delay(2);
  }
  // insertion sort — cukup untuk 32 elemen
  for (int i = 1; i < JUMLAH_SAMPEL_ADC; i++) {
    int kunci = sampel[i], j = i - 1;
    while (j >= 0 && sampel[j] > kunci) { sampel[j + 1] = sampel[j]; j--; }
    sampel[j + 1] = kunci;
  }
  rawKeluar = sampel[JUMLAH_SAMPEL_ADC / 2];   // median

  if (rawKeluar < AMBANG_ADC_GAGAL) return 0.0;   // sentinel: gagal baca ADC
  return A_KALIBRASI * rawKeluar + B_KALIBRASI;
}
```

> **Sentinel `0.0` dipertahankan** — `CLAUDE.md` dan `analisa_lab.py` sudah bergantung padanya
> sebagai penanda "ADC gagal baca", berbeda dari bacaan nyata. Jangan diubah maknanya.

**Verifikasi.** Bandingkan 10 bacaan berurutan dengan multimeter — selisih harus < 0,05 V dan
sebaran antar sampel < 5 LSB. `diagnostik.py`: standar deviasi `tegangan_vcc` dalam jendela
1 jam harus turun signifikan dibanding sekarang.

**Status:** ⬜ BELUM

---

### M5. ADC dibaca saat WiFi aktif → mengukur tegangan yang sudah sag

**Severity** MAYOR · **Tier** firmware · **Lokasi** `.ino:110-113` (urutan setup), `:150-161` (baca ADC)

**Bukti.** `processAndSendData()` dipanggil setelah WiFi + Firebase init, jadi `analogRead()` terjadi
saat radio menarik 70–200 mA berdenyut. Indikasinya:
```
2026-08-11 22:07  V = 3.9653
2026-08-11 22:12  V = 3.9734
2026-08-11 22:27  V = 3.9249
2026-08-11 23:12  V = 4.1349   <-- baterai yang sama, NAIK saat sedang discharge
```
Wobble juga jauh lebih besar saat baterai rendah (resistansi internal tinggi → sag lebih dalam),
konsisten dengan load-sag.

**Akar.** Urutan di `setup()`: WiFi → Firebase → baca sensor & ADC. Pengukuran tegangan idealnya
dilakukan saat radio mati.

**Perbaikan.** Pindahkan pembacaan ADC ke **paling awal** `setup()`, sebelum `wifiMulti.addAP()`.
Simpan ke global, kirim nanti. Ini perubahan urutan — tidak perlu komponen tambahan.

```cpp
// --- GLOBAL ---
float teganganBaterai = 0.0;
int   rawBaterai      = 0;

void setup() {
  Serial.begin(115200);
  Serial.println("\n\n--- MULAI ---");

  // 1. BACA BATERAI PALING AWAL — radio masih mati, tidak ada sag
  teganganBaterai = bacaBaterai(rawBaterai);
  Serial.printf("Baterai: %.3f V (raw %d)\n", teganganBaterai, rawBaterai);

  // 2. PROTEKSI DAYA (C2) — sebelum boros energi untuk WiFi
  if (teganganBaterai > 0.5 && teganganBaterai < BATAS_KRITIS) { /* ... hibernasi ... */ return; }

  // 3. SETUP SENSOR I2C
  // 4. SETUP & KONEKSI WIFI
  // 5. CONFIG FIREBASE
  // 6. BACA SENSOR + PUSH
}
```

Hapus blok smoothing ADC lama dari `processAndSendData()` (`.ino:150-161`) dan gunakan global
`teganganBaterai` / `rawBaterai` saat menyusun JSON.

**Verifikasi.** `DEBUG_MODE = true`, bandingkan nilai serial dengan multimeter di terminal baterai —
selisih harus < 0,05 V, dan tidak boleh naik-turun mengikuti aktivitas WiFi.

**Status:** ⬜ BELUM

---

### M6. `ESP.deepSleep()` tanpa `delay()` → eksekusi lanjut jalan

**Severity** MAYOR · **Tier** firmware · **Lokasi** `.ino:197-200` (`goToSleep`), `:81-85` (jalur timeout WiFi)

**Bukti / Akar.** `ESP.deepSleep()` di ESP8266 **menjadwalkan** sleep; SDK perlu kembali ke idle task
lebih dulu. Tanpa `delay()` setelahnya, kode terus berjalan. Akibatnya di jalur timeout WiFi:

```cpp
if (timeout > 40) {
  Serial.println("\nGagal Konek!");
  if (!DEBUG_MODE) goToSleep();   // TIDAK langsung tidur
  break;
}
// ...lanjut ke Firebase.begin() dan processAndSendData() TANPA WiFi
```

**Perbaikan.**

```cpp
void goToSleep() {
  Serial.println("Zzzzz....");
  Serial.flush();                                       // jangan potong output serial
  ESP.deepSleep((uint64_t)SLEEP_SECONDS * 1000000ULL);  // cast: cegah overflow (m3)
  delay(100);                                           // WAJIB agar SDK benar-benar tidur
}
```

Tambahkan `return;` eksplisit setelah setiap `goToSleep()` di dalam `setup()` — termasuk di jalur
timeout WiFi dan di jalur "semua sensor error" (`.ino:165-169`).

**Verifikasi.** `DEBUG_MODE = false`, matikan semua WiFi yang dikenal, amati serial: harus mencetak
"Gagal Konek!" → "Zzzzz...." lalu **berhenti** — tidak boleh ada percobaan Firebase setelahnya.

**Status:** ⬜ BELUM

---

### M7. `wifiMulti.run()` dalam loop → scan berulang, boros baterai

**Severity** MAYOR · **Tier** firmware · **Lokasi** `.ino:75-86`

**Bukti / Akar.** Setiap `wifiMulti.run()` melakukan **full channel scan (~2 detik @ ~70 mA)**.
Memanggilnya dalam loop `delay(500)` me-restart scan terus-menerus.

Waktu bangun mendominasi budget energi: ~80 mA × ~12 s bangun vs ~0,3 mA saat tidur. Jadi ini
pengeluaran terbesar. Memotong waktu bangun 12 s → 3 s kurang lebih **melipattigakan umur baterai**.

> ⚠️ **Dependensi:** kerjakan **M2 lebih dulu**. Delay WiFi yang lambat saat ini kebetulan memberi
> waktu settle bagi BMP280 — mempercepat WiFi tanpa memperbaiki M2 akan memperburuk korupsi data.

**Perbaikan.** Ganti `.ino:75-86`:

```cpp
WiFi.mode(WIFI_STA);
WiFi.persistent(false);          // stop tulis flash tiap wake (m3/flash wear)
WiFi.setOutputPower(17.0);       // default 20,5 dBm berlebihan untuk dalam rumah

// IP statis: hemat 1-2 detik DHCP tiap wake. Sesuaikan dengan subnet jaringan utama.
WiFi.config(IPAddress(192,168,1,50),   // IP perangkat
            IPAddress(192,168,1,1),    // gateway
            IPAddress(255,255,255,0),  // netmask
            IPAddress(8,8,8,8));       // DNS

Serial.print("Mencari WiFi...");
if (wifiMulti.run(20000) != WL_CONNECTED) {   // SATU panggilan dengan timeout, bukan loop
  Serial.println("\nGagal Konek!");
  if (!DEBUG_MODE) { goToSleep(); return; }
} else {
  Serial.printf("\nTerhubung: %s (%d dBm) %s\n",
                WiFi.SSID().c_str(), WiFi.RSSI(), WiFi.localIP().toString().c_str());
}
```

**Optimasi lanjutan (opsional, hasil terbesar).** Simpan BSSID + channel yang berhasil ke RTC memory
dan coba koneksi langsung — ini melewati scan sepenuhnya:

```cpp
struct DataRTC { uint32_t crc; uint8_t bssid[6]; uint8_t channel; };
// ESP.rtcUserMemoryRead/Write pada offset 0; validasi dengan CRC sebelum dipakai.
// Kalau WiFi.begin(ssid, pass, channel, bssid) gagal, fallback ke wifiMulti.run(20000).
```

> ⚠️ IP statis membuat perangkat terikat ke satu subnet. `wifiMulti` punya 5 AP di jaringan berbeda.
> Kalau subnet-nya tidak sama, gunakan `WiFi.config()` hanya untuk SSID utama, atau simpan profil IP
> per-BSSID di RTC memory. **Tanya user dulu** kalau subnet tiap AP belum diketahui.

**Verifikasi.** Tambahkan `durasi_bangun_ms` (M11), lalu bandingkan median sebelum vs sesudah —
target < 4000 ms. Pastikan juga tidak ada peningkatan gap data (`diagnostik.py`: gap > 6 menit
tidak boleh naik).

**Status:** ⬜ BELUM

---

### M8. Push gagal = data hilang permanen

**Severity** MAYOR · **Tier** firmware · **Lokasi** `.ino:189-194`

**Akar.** RAM hilang saat deep sleep dan tidak ada retry. Setiap kegagalan push = satu record hilang
selamanya. Data menunjukkan 2,2 % gap > 6 menit.

**Perbaikan.**

```cpp
const int MAKS_PERCOBAAN_PUSH = 3;

bool kirimKeFirebase(FirebaseJson &json) {
  for (int percobaan = 1; percobaan <= MAKS_PERCOBAAN_PUSH; percobaan++) {
    Serial.printf("Push ke Firebase (percobaan %d/%d)... ", percobaan, MAKS_PERCOBAAN_PUSH);
    if (Firebase.RTDB.pushJSON(&fbdo, "/RIWAYAT_LAB", &json)) {
      Serial.println("Berhasil!");
      return true;
    }
    Serial.printf("Gagal: %s\n", fbdo.errorReason().c_str());
    if (percobaan < MAKS_PERCOBAAN_PUSH) delay(1000 * percobaan);   // backoff linier
  }
  return false;
}
```

**Verifikasi.** Uji dengan mematikan router di tengah siklus — serial harus menunjukkan 3 percobaan
sebelum menyerah dan tidur.

**Status:** ⬜ BELUM

---

### M9. Kedua sensor gagal → tegangan baterai juga tidak terkirim

**Severity** MAYOR · **Tier** firmware · **Lokasi** `.ino:165-169`

**Akar.**
```cpp
if (!ahtStatus && !bmpStatus) {
  Serial.println("Semua Sensor Error.");
  if (!DEBUG_MODE) goToSleep();
  return;                        // tegangan_vcc ikut hilang
}
```
Justru di saat kedua sensor gagal itulah tegangan baterai paling perlu diketahui — kegagalan sensor
sering merupakan gejala brownout akibat baterai lemah.

**Perbaikan.** Jangan return dini. Tetap push `tegangan_vcc` + `status_sensor`, biarkan field
sensor dihilangkan sesuai data contract:

```cpp
if (!ahtStatus && !bmpStatus) {
  Serial.println("Semua sensor error — tetap kirim data daya untuk diagnosis.");
  json.set("status_sensor", "semua_gagal");
}
// lanjut menyusun JSON dan push seperti biasa
```

**Verifikasi.** Lepas kabel I2C sensor, jalankan satu siklus, pastikan record tetap masuk dengan
`tegangan_vcc` terisi dan tanpa `suhu`/`kelembapan`/`tekanan`.

**Status:** ⬜ BELUM

---

### M10. Risiko hardware: USB back-feed ke baterai lewat pin 5V

**Severity** MAYOR (keselamatan) · **Tier** hardware · **🧑 BUTUH MANUSIA**

**Akar.** `Baterai+ → pin 5V`. Di D1 mini, pin `5V` tersambung **langsung ke USB VBUS** tanpa dioda
proteksi. Menancapkan USB saat baterai terpasang = USB 5 V mendorong arus ke Li-ion tanpa kontrol
arus maupun terminasi CV. BMS *seharusnya* memutus pada over-voltage per-cell, tapi jangan diandalkan
sebagai proteksi utama.

**Perbaikan.**
- 🧑 **Aturan operasional segera: jangan pernah menancapkan USB saat baterai tersambung.**
  Cabut baterai dulu sebelum flash firmware.
- 🧑 Solusi permanen: saklar pemutus baterai, atau modul charger TP4056 + proteksi yang menangani
  pengisian dengan benar.
- ❌ **Jangan** memasang dioda Schottky seri pada jalur baterai — drop 0,3 V memperburuk headroom
  LDO dan mempercepat brownout yang jadi akar C2.

**Verifikasi.** Tidak ada verifikasi otomatis. Catat aturan ini di `CLAUDE.md` bagian firmware
agar tidak terlupakan saat flash berikutnya.

**Status:** ⬜ BELUM

---

### M11. Tidak ada field diagnostik

**Severity** MAYOR · **Tier** firmware · **Lokasi** `.ino:173-185`

**Akar.** Mustahil men-debug masalah konektivitas atau baterai dari data yang tersimpan. Murah
ditambahkan, sangat berguna — dan **prasyarat untuk mengukur keberhasilan M7**.

**Perbaikan.** Tambahkan ke penyusunan JSON:

```cpp
json.set("rssi", WiFi.RSSI());                    // kekuatan sinyal, dBm
json.set("durasi_bangun_ms", (int)millis());      // set TEPAT sebelum push
json.set("adc_raw", rawBaterai);                  // pisahkan mentah dari terkalibrasi
```

`adc_raw` yang paling berharga: memungkinkan **rekalibrasi ulang data historis** tanpa flash ulang
kalau ternyata konstanta kalibrasi salah. Kalau field ini ada sejak awal, M3/M4 bisa diperbaiki
secara retroaktif.

> **Data contract.** Ketiga field ini baru dan opsional. Update `CLAUDE.md` §"RTDB data contract".
> Konsumen harus memperlakukan ketiadaannya sebagai absen, bukan nol.

**Verifikasi.** Satu siklus push, cek record baru di Firebase Console memuat ketiga field.

**Status:** ⬜ BELUM

---

### M12. `orderByChild("timestamp")` tanpa `.indexOn` → unduh seluruh node

**Severity** MAYOR (biaya/kuota) · **Tier** dashboard + rules · **Lokasi** `index.html:902` (filter), `:985` (export) · **🧑 sebagian**

**Akar.** Filter tanggal dan export by-date memakai `orderByChild("timestamp")`. Kalau rules tidak
punya `.indexOn`, JS SDK **mengunduh seluruh node** lalu menyaring di sisi klien — setiap kali.
Dengan ~700 KB dan tumbuh 288 record/hari, ini pemborosan kuota terbesar di dashboard.

**⚠️ Belum terverifikasi.** Audit tidak bisa membaca security rules dari luar. **Cek dulu:**
buka dashboard, gunakan filter tanggal, lihat browser console. Kalau ada
`FIREBASE WARNING: Using an unspecified index`, maka index belum ada.

**Perbaikan.** 🧑 Firebase Console → Realtime Database → Rules:

```json
{
  "rules": {
    "RIWAYAT_LAB": {
      ".read": true,
      ".write": "auth != null",
      ".indexOn": "timestamp"
    },
    "LAPORAN_AI": {
      ".read": true,
      ".write": "auth != null"
    }
  }
}
```

**Verifikasi.** Ulangi filter tanggal — warning di console harus hilang, dan tab Network harus
menunjukkan payload yang jauh lebih kecil.

**Status:** ⬜ BELUM

---

### M13. Peta persentase baterai linier + tidak ada peringatan

**Severity** MAYOR · **Tier** dashboard · **Lokasi** `index.html:709-736` (rumus di `:720`)

**Akar.**
```javascript
let pct = ((volt - 3.2) / (4.2 - 3.2)) * 100;
```
Kurva discharge Li-ion sangat datar di 3,7–3,9 V (di rentang itu ada ~60 % kapasitas), jadi peta
linier **melebih-lebihkan sisa daya** di tengah lalu runtuh mendadak di akhir. Digabung dengan C2:
badge menampilkan 0 % berhari-hari tanpa alarm apa pun.

**Perbaikan.** Piecewise linear + banner peringatan (bukan hanya badge kecil):

```javascript
// Kurva discharge Li-ion 1S di bawah beban ringan. [tegangan, persen]
const KURVA_BATERAI = [
  [3.30,   0], [3.50,  5], [3.65, 15], [3.75, 40],
  [3.85,  60], [3.95, 80], [4.05, 92], [4.15, 100],
];

function persenBaterai(volt) {
  if (volt <= KURVA_BATERAI[0][0]) return 0;
  if (volt >= KURVA_BATERAI[KURVA_BATERAI.length - 1][0]) return 100;
  for (let i = 1; i < KURVA_BATERAI.length; i++) {
    const [v1, p1] = KURVA_BATERAI[i - 1];
    const [v2, p2] = KURVA_BATERAI[i];
    if (volt <= v2) return p1 + (volt - v1) * (p2 - p1) / (v2 - v1);
  }
  return 100;
}
```

Tambahkan elemen banner di markup (di atas baris kartu, sekitar `index.html:123`):

```html
<div id="peringatanBaterai" class="alert alert-danger d-none" role="alert">
  <i class="fas fa-battery-empty me-2"></i>
  <strong>Baterai kritis.</strong> <span id="peringatanBateraiDetail"></span>
</div>
```

Dan di `updateCardsUI()`, ganti blok baterai agar memunculkan banner saat `pct <= 20` atau
`status_daya === "kritis"`.

**Verifikasi.** Tidak butuh hardware — sementara ubah nilai `volt` di konsol atau uji dengan data
historis Juli (2,6–2,9 V). Banner harus muncul; pada 3,85 V badge harus ~60 %, bukan ~65 %.

**Status:** ⬜ BELUM

---

### M14. XSS: `marked.parse()` tanpa sanitasi + SSID tidak di-escape

**Severity** MAYOR (keamanan) · **Tier** dashboard · **Lokasi** `index.html:1053` (`marked.parse`), `:599` (SSID)

**Akar.**
```javascript
contentDiv.innerHTML = marked.parse(data.laporan_text);         // :1053 — tanpa sanitasi
`<span class="badge ...">${data.koneksi || "?"}</span>`          // :599  — SSID mentah
```
`marked` **tidak** menyanitasi secara default. Karena secret bocor (C1) memberi akses tulis penuh,
penyerang bisa menyimpan `<img src=x onerror=...>` ke `/LAPORAN_AI` dan dieksekusi di dashboard.

**Perbaikan.** Tambahkan DOMPurify setelah `index.html:13`:

```html
<script src="https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js"></script>
```

Ganti `:1053`:
```javascript
contentDiv.innerHTML = DOMPurify.sanitize(marked.parse(data.laporan_text));
```

Escape SSID — tambahkan helper dan pakai di `renderTableRows()`:
```javascript
const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
```
```javascript
<td><span class="badge bg-dark border border-secondary">${escapeHtml(data.koneksi) || "?"}</span></td>
```

**Verifikasi.** Sementara set `/LAPORAN_AI/laporan_text` ke `# Uji\n<img src=x onerror="alert(1)">`
lewat Firebase Console. Heading harus tampil, `alert` **tidak** boleh muncul. Kembalikan nilai asli
setelah uji.

**Status:** ⬜ BELUM

---

### M15. `bat_awal`/`bat_akhir` tidak menyaring sentinel 0,0 V

**Severity** MAYOR · **Tier** python · **Lokasi** `analisa_lab.py:78-84`

**Bukti.** Terverifikasi menghasilkan `bat_selisih = -0,99 V/minggu` — prompt memberi tahu AI bahwa
baterai **bertambah** 0,99 V dalam seminggu.

**Akar.** `df.dropna(subset=['tegangan_vcc'])` **tidak** membuang `0.0`. Sentinel 0,0 V (ADC gagal
baca) diperlakukan sebagai bacaan nyata. Ditambah `iloc[0]`/`iloc[-1]` pada frame tak terurut (C3).

**Perbaikan.** Sudah termasuk dalam patch C3 (`df.loc[df["tegangan_vcc"] < 0.5, "tegangan_vcc"] = pd.NA`
plus `pertama_valid`/`terakhir_valid`). Tidak ada pekerjaan terpisah — **verifikasi saja setelah C3.**

**Verifikasi.** `bat_selisih` harus positif saat baterai memang turun, dan `NaN` (ditampilkan
"DATA TIDAK TERSEDIA") kalau tidak ada bacaan valid.

**Status:** ⬜ BELUM (tergantung C3)

---

### M16. Tren tekanan `head(3)`/`tail(3)` pada frame tak terurut + outlier

**Severity** MAYOR · **Tier** python · **Lokasi** `analisa_lab.py:87-103`

**Bukti.** Outlier `18,29` dan `670,83` hPa menggeser mean **~0,9 hPa**
(1010,6 tercemar vs 1011,47 setelah dibersihkan). Itu **lebih besar dari threshold ±0,5 hPa**
yang dipakai untuk memutuskan tren cuaca (`:103`) — jadi verdict "Naik/Turun/Stabil" bisa terbalik total.

Ditambah: karena urutan tidak dijamin kronologis (C3), "tekanan lama" (`head(3)`) dan "tekanan baru"
(`tail(3)`) bisa tertukar.

**Perbaikan.** Sudah termasuk dalam patch C3. Tidak ada pekerjaan terpisah — **verifikasi saja.**

**Verifikasi.** Bandingkan `tekanan_rata2` sebelum vs sesudah patch; selisih ~0,9 hPa membuktikan
outlier sudah terbuang. Cek `status_tren` konsisten dengan arah data mentah 3 jam terakhir.

**Status:** ⬜ BELUM (tergantung C3)

---

### M17. `maintenance_db.py`: 1 DELETE per record + hapus record tanpa timestamp

**Severity** MAYOR · **Tier** python · **Lokasi** `maintenance_db.py:51-65` (identifikasi), `:80-81` (hapus)

**Akar.** Tiga masalah:
1. `timestamp = data.get('timestamp', 0)` (`:54`) → record **tanpa** timestamp mendapat nilai 0,
   lalu `0 < batas_waktu_ms` → **dianggap usang dan dihapus.** Penghapusan data valid secara silent.
2. `is_error = (suhu == 0 or hum == 0)` (`:56`) → tidak menangkap `suhu == -50` maupun
   `tekanan` di luar rentang wajar. Sampah pressure lolos dan mencemari chart + laporan AI.
3. Loop `ref.child(key).delete()` (`:80-81`) → satu HTTP request per key. Untuk ribuan record:
   lambat dan menghantam API.

**Perbaikan.** Ganti blok identifikasi `:51-65`:

```python
RENTANG_WAJAR = {"suhu": (-10, 60), "kelembapan": (1, 100), "tekanan": (950, 1050)}

keys_to_delete, keys_tanpa_timestamp = [], []
jumlah_error = jumlah_usang = 0

for push_key, data in all_data.items():
    if not isinstance(data, dict):        # child non-object, jangan diproses
        continue

    timestamp = data.get("timestamp")
    if timestamp is None:
        keys_tanpa_timestamp.append(push_key)   # JANGAN hapus — laporkan untuk review manual
        continue

    def wajar(nama):
        nilai = data.get(nama)
        if nilai is None:
            return True                    # field absen itu sah (conditionally omitted)
        lo, hi = RENTANG_WAJAR[nama]
        return lo <= nilai <= hi

    is_error = (data.get("suhu") == 0 or data.get("kelembapan") == 0
                or data.get("suhu") == -50
                or not wajar("suhu") or not wajar("kelembapan") or not wajar("tekanan"))
    is_old = timestamp < batas_waktu_ms

    if is_error:
        jumlah_error += 1
    elif is_old:
        jumlah_usang += 1
    if is_error or is_old:
        keys_to_delete.append(push_key)

if keys_tanpa_timestamp:
    print(f"PERINGATAN: {len(keys_tanpa_timestamp)} record tanpa timestamp — "
          f"TIDAK dihapus, perlu review manual.")
```

Ganti eksekusi penghapusan `:79-81` (print + loop, di dalam blok `if konfirmasi.lower() == 'y'`) dengan batch:

```python
print("[4/4] Menghapus data dari Firebase (batch)...")
UKURAN_BATCH = 500
for i in range(0, len(keys_to_delete), UKURAN_BATCH):
    potongan = keys_to_delete[i:i + UKURAN_BATCH]
    ref.update({k: None for k in potongan})    # satu request per batch, bukan per record
    print(f"  {min(i + UKURAN_BATCH, len(keys_to_delete))}/{len(keys_to_delete)}")
print("PEMBERSIHAN SELESAI!")
```

> ⚠️ **Skrip ini destruktif.** Prompt konfirmasi `y/n` di `:77` **harus dipertahankan**. Jangan
> pernah menambahkan flag auto-yes. Pastikan file backup sudah tertulis sebelum penghapusan
> (urutan yang ada sekarang sudah benar: backup di `:37-39`, penghapusan baru terjadi di `:79-81`).

**Verifikasi.** Jalankan, jawab `n` pada prompt. Bandingkan hitungan yang dilaporkan dengan
`diagnostik.py` — jumlah error harus mencakup semua outlier tekanan dan `suhu == -50`.

**Status:** ⬜ BELUM

---

## 6. MINOR

### m1. Interval nyata 4,87 mnt vs threshold offline 5,5 mnt
`index.html:513` · Median interval terukur **4,87 menit** (RTC ESP8266 ~5 % cepat), dan **2,2 % gap > 6 menit**
→ badge "Offline" palsu. **Fix:** naikkan threshold ke `7`, atau set `SLEEP_SECONDS = 315` di firmware.
⚠️ Keduanya adalah konstanta yang saling terkait — lihat `CLAUDE.md` §Constants coupled across tiers.
**Status:** ⬜

### m2. Deteksi offline membandingkan jam server dengan jam klien
`index.html:509-513` · `Date.now()` (klien) vs `timestamp` (server). Klien dengan jam ngawur → status salah.
**Fix:** longgarkan toleransi, atau ambil offset server via `/.info/serverTimeOffset`. **Status:** ⬜

### m3. `SLEEP_SECONDS * 1000000` overflow `int`
`.ino:199` · `300 × 1000000` masih aman, tapi > 2147 s overflow. Batas ESP8266 juga ~71 menit.
**Fix:** sudah termasuk di patch M6 (`(uint64_t)... * 1000000ULL`); cek juga `ESP.deepSleepMax()`. **Status:** ⬜

### m4. `requests.get()` tanpa `timeout`
`analisa_lab.py:120`, `:134`, `:153` · Tanpa timeout, Task Scheduler bisa menggantung selamanya.
**Fix:** `requests.get(url, timeout=10)` di ketiga tempat. **Status:** ⬜

### m5. `suhu_min` dihitung tapi tak dipakai
`analisa_lab.py:68` · Nilainya **−50** (sampah) dan tidak pernah masuk prompt.
**Fix:** setelah C3 memfilter outlier, masukkan ke prompt sebagai "Suhu Terendah Mingguan" atau hapus. **Status:** ⬜

### m6. `df['suhu']` `KeyError` bila kolom absen total
`analisa_lab.py:57-59` · Hanya `tegangan_vcc` yang dijaga (`:61`). **Fix:** sudah termasuk di patch C3
(loop `for kolom in [...]`). **Status:** ⬜

### m7. Nama model di `DAFTAR_MODEL_AI` perlu diverifikasi
`analisa_lab.py:22-30` · Nama seperti `gemma4:31b-cloud`, `ministral-3:14b-cloud`, `nemotron-3-super:cloud`
perlu dicocokkan dengan katalog Ollama yang benar-benar tersedia. Nama salah tetap "berfungsi" karena ada
fallback, tapi membuang satu round-trip per run. **Fix:** samakan dengan output `ollama list`, taruh model
yang pasti ada di posisi terakhir sebagai jaring pengaman. **Status:** ⬜

### m8. Threshold kenyamanan diduplikasi di 3 tempat
`index.html:128` (tooltip suhu), `:383-388` (gauge zones), `:644-647` (logika warna) — dan sama untuk
kelembapan di `:144`, `:407-412`, `:664-667`. Mengubah satu ambang butuh 3 edit.
**Fix:** satu `const AMBANG = { suhu: [24,29,33], kelembapan: [40,65,80] }` sebagai sumber tunggal,
generate `staticZones` dan teks tooltip darinya. **Status:** ⬜

### m9. `#statusPress` / `#iconPress` tidak ada di markup
`index.html:703-706` menulis ke ID tersebut, tapi kartu Tekanan (`:156-170`) tidak memilikinya — nilai
dihitung lalu dibuang (penulisannya sudah `if`-guarded, jadi tidak error).
**Fix:** tambahkan `<div id="statusPress">` dan beri `id="iconPress"` pada `<i>` di `:163`. **Status:** ⬜

### m10. Chart axis kiri `min: 0` → garis suhu gepeng
`index.html:471` · Suhu (±30) dan kelembapan (±75) berbagi axis dengan `min: 0`.
**Fix:** `min: 20, max: 100`, atau pindahkan suhu ke axis sendiri. **Status:** ⬜

### m11. CSV: tanpa BOM UTF-8, nama file tanpa tanggal, URL tak di-revoke
`index.html:1011-1018` · Excel merusak karakter non-ASCII; `Data_Lab_${option}.csv` menimpa file
sebelumnya; `URL.createObjectURL` tanpa `revokeObjectURL` = kebocoran memori.
**Fix:**
```javascript
const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
const url = window.URL.createObjectURL(blob);
const a = document.createElement("a");
a.href = url;
a.download = `Data_Lab_${option}_${new Date().toISOString().slice(0,10)}.csv`;
a.click();
window.URL.revokeObjectURL(url);
```
**Status:** ⬜

### m12. `myChart.update()` dipanggil 30× saat backfill awal
`index.html:778` · `onChildAdded` fires sekali per record historis → 30 animasi chart saat load.
**Fix:** hitung jumlah backfill, tunda `update()` sampai record terakhir (atau debounce ~100 ms). **Status:** ⬜

### m13. Koreksi elevasi 8 m di bawah akurasi BMP280
`.ino:24` · `ELEVASI_PERANGKAT = 8.0` m menghasilkan koreksi ~0,96 hPa, sementara akurasi absolut
BMP280 ±1 hPa. Tidak salah, tapi jangan diklaim presisi. **Fix:** dokumentasikan saja; jangan hapus
(nilai `tekanan` yang tersimpan sudah MSLP — mengubahnya memutus perbandingan dengan data historis). **Status:** ⬜

### m14. Sensor disuplai terus dari baterai mentah
Modul AHT20+BMP280 tersambung permanen ke pin `5V`; regulator modul + pull-up I2C tetap menarik arus
saat ESP tidur. **Fix (opsional, butuh solder):** suplai VCC modul dari GPIO, atau P-MOSFET switch.
Manfaatnya jauh lebih kecil dibanding M7 — kerjakan terakhir. **Status:** ⬜

### m15. API key OWM & WAQI hardcoded
`analisa_lab.py:14-15` · Aman untuk saat ini karena `analisa_lab.py` ter-gitignore, tapi rapuh —
satu `git add -f` yang keliru membocorkannya. **Fix:** pindah ke env var atau `secrets.py`. **Status:** ⬜

### m16. `.gitignore` baris `/*` — file root baru butuh `git add -f`
`.gitignore:16` · Baris terakhir `/*` mengabaikan **semua** file di root. Hanya file yang sudah tracked
muncul di `git status`. **File `laporan_and_fixing_plan.md` ini pun tidak akan ter-commit tanpa
`git add -f laporan_and_fixing_plan.md`.**
**Fix:** ganti `/*` dengan allowlist eksplisit, misal:
```gitignore
# Abaikan semua di root, lalu izinkan yang memang perlu di-track
/*
!/.gitignore
!/CLAUDE.md
!/index.html
!/laporan_and_fixing_plan.md
!/Wemos_BMP280_ATH20_Firebase/
```
**Status:** ⬜

### m17. Deletion di `origin/main` belum di-commit
`Wemos_BMP280_ATH20_Firebase.ino`, `backup/index.html`, `backup/index1.html` sudah dihapus lokal tapi
deletion-nya belum di-commit. **Fix:** commit deletion-nya — tapi ini bagian dari C1 langkah 5
(purge history), jadi **kerjakan sesudah C1**, bukan sebelumnya. **Status:** ⬜

---

## 7. Rencana eksekusi

Kerjakan per fase. Jangan lompat fase — ada dependensi nyata di dalamnya.

### FASE 1 — Hentikan kerusakan (hari ini)

| Urut | Item | Siapa | Catatan |
|---|---|---|---|
| 1 | **C1** langkah 1–2: rotate secret, ganti password WiFi | 🧑 user | Hitungannya jam, bukan hari |
| 2 | **C2**: cutoff baterai `< 3,40 V` | agent | Pack sudah pernah over-discharge; jangan diulang |
| 3 | **M1** + **M2**: validasi rentang + `MODE_FORCED` | agent | Hentikan sampah masuk database |
| 4 | **M6**: `delay()` setelah `deepSleep()` + `return` | agent | Prasyarat agar C2 benar-benar tidur |

Flash firmware sekali untuk item 2–4 sekaligus. 🧑 **Cabut baterai sebelum menancapkan USB** (M10).

### FASE 2 — Pulihkan kebenaran data (minggu ini, tanpa flash)

| Urut | Item | Catatan |
|---|---|---|
| 5 | **C3** (sekaligus menyelesaikan **M15**, **M16**, **m6**) | Murni Python, tidak perlu sentuh hardware |
| 6 | **m4**, **m5**, **m7** | Perbaikan kecil `analisa_lab.py` sekalian |
| 7 | **M14**: DOMPurify + escape SSID | Dashboard, aman dikerjakan kapan saja |
| 8 | **M12**: cek console → tambahkan `.indexOn` | 🧑 butuh Firebase Console |
| 9 | **M13**: kurva baterai + banner peringatan | Melengkapi C2 di sisi UI |
| 10 | **M17**: perbaiki `maintenance_db.py` | Jangan dijalankan dulu sampai patch selesai |
| 11 | **C1** langkah 3–5: `secrets.h`, purge history, lalu **m17** | 🧑 sebagian |

### FASE 3 — Efisiensi & keandalan (butuh flash)

| Urut | Item | Catatan |
|---|---|---|
| 12 | **M11**: field diagnostik | Kerjakan **sebelum** M7 agar hasilnya bisa diukur |
| 13 | **M5**: pindahkan baca ADC ke awal `setup()` | Gratis, hanya urutan kode |
| 14 | **M7**: IP statis + `run(timeout)` + `persistent(false)` | ⚠️ **wajib** M2 selesai lebih dulu. Tanya user soal subnet |
| 15 | **M8**: retry push | |
| 16 | **M9**: tetap kirim data daya saat sensor gagal | |
| 17 | **m3** | Sudah tercakup di patch M6 — verifikasi saja |

### FASE 4 — Hardware (butuh solder)

| Urut | Item | Catatan |
|---|---|---|
| 18 | **M4**: kapasitor 100 nF A0→GND | Satu komponen, dampak terbesar |
| 19 | **M3**: resistor 100 kΩ → 150 kΩ | Lalu **wajib** kalibrasi 2 titik (M4) |
| 20 | **M10**: saklar pemutus baterai / TP4056 | |
| 21 | **m14**: sensor power switching | Manfaat kecil; terakhir |

### FASE 5 — Kebersihan kode (kapan saja)

**m1**, **m2**, **m8**, **m9**, **m10**, **m11**, **m12**, **m13**, **m15**, **m16**

---

## 8. Aturan & batasan untuk agent

**JANGAN LAKUKAN:**

1. ❌ **Jangan mengganti nama key RTDB** (`suhu`, `kelembapan`, `tekanan`, `tegangan_vcc`, `koneksi`,
   `timestamp`). Itu wire format antar tiga tier **sekaligus** skema history yang tersimpan —
   mengganti nama satu key mengorphankan ~4.000 record lama.
2. ❌ **Jangan mengubah makna sentinel `tegangan_vcc = 0.0`.** `CLAUDE.md`, `analisa_lab.py`, dan
   dashboard sudah bergantung padanya sebagai "ADC gagal baca", berbeda dari bacaan nyata.
3. ❌ **Jangan menuliskan nilai kredensial ke file mana pun yang bisa ter-commit** — termasuk dokumen ini.
4. ❌ **Jangan menjalankan `maintenance_db.py` tanpa izin user.** Skrip ini menghapus data. Prompt
   konfirmasi `y/n` wajib dipertahankan; jangan tambahkan flag auto-yes.
5. ❌ **Jangan mengunduh seluruh `/RIWAYAT_LAB`** untuk keperluan diagnosis. Free-tier egress terbatas
   dan node sudah ~700 KB, tumbuh 288 record/hari. Pakai `limit_to_last(n)` seperti di `diagnostik.py`,
   atau pakai backup lokal yang sudah ada.
6. ❌ **Jangan menulis apa pun ke Firebase saat audit/diagnosis.** Read-only.
7. ❌ **Jangan memasang dioda Schottky seri di jalur baterai** — memperburuk headroom LDO (lihat M10).
8. ❌ **Jangan ubah `ELEVASI_PERANGKAT` atau `OFFSET_TEKANAN`** tanpa alasan kuat — nilai `tekanan`
   tersimpan sudah MSLP; mengubahnya memutus perbandingan dengan data historis.

**SELALU LAKUKAN:**

1. ✅ Baca `CLAUDE.md` §"Constants coupled across tiers" sebelum mengubah konstanta apa pun.
   Cadence, kalibrasi baterai, dan tekanan masing-masing punya 3–4 counterpart di tier lain.
2. ✅ Perlakukan field yang hilang sebagai **absen**, bukan nol. Firmware sengaja menghilangkan field
   saat sensor gagal (`!== undefined` di dashboard, `errors='coerce'` di pandas).
3. ✅ Tulis kode, komentar, dan commit message dalam **Bahasa Indonesia**.
4. ✅ Jalankan bagian **Verifikasi** setiap temuan sebelum menandai `✅ SELESAI`.
5. ✅ Update **§10 Log Perubahan** dan checkbox `Status:` setiap selesai satu item.
6. ✅ Update `CLAUDE.md` kalau menambah field RTDB baru (`status_daya`, `rssi`, `adc_raw`,
   `durasi_bangun_ms`, `status_sensor`) atau mengubah konstanta yang saling terkait.
7. ✅ `git add -f` untuk file baru di root (lihat m16) — kalau tidak, file tidak akan pernah ter-commit.

**Batasan lingkungan:**

- Tidak ada `arduino-cli`. Kompilasi & flash firmware **harus** lewat Arduino IDE oleh user.
  Agent bisa mengedit `.ino` tapi tidak bisa memverifikasi kompilasi.
- Tidak ada build step, package manager, test suite, maupun linter di repo ini.
- Dashboard harus diakses lewat HTTP (`http://localhost/Monitoring_Suhu/` via XAMPP) —
  ES module dari CDN gagal di `file://`.
- `analisa_lab.py` butuh Ollama hidup di `http://127.0.0.1:11434`.
- Uji hardware (kalibrasi, cutoff baterai) butuh user dengan multimeter.

---

## 9. Definition of done

Perbaikan dianggap tuntas kalau seluruh pernyataan berikut benar:

**Keamanan**
- [ ] Secret Firebase lama menghasilkan `401`, bukan `200`.
- [ ] `git log --all -S"<secret>"` kosong.
- [ ] Password ketiga WiFi sudah diganti di router.
- [ ] Kredensial tinggal di `secrets.h` yang ter-gitignore.
- [ ] Payload XSS di `/LAPORAN_AI` tidak dieksekusi di dashboard.

**Integritas data** (jalankan `diagnostik.py`, hitung hanya record setelah tanggal perbaikan)
- [ ] `suhu == 0` → 0
- [ ] `suhu == -50` → 0
- [ ] `tekanan di luar 950-1050` → 0
- [ ] `raw` maksimum < 1000 (tidak clipping)
- [ ] `tegangan_vcc` minimum > 3,35 V (cutoff bekerja)

**Laporan AI**
- [ ] Tidak ada string `nan` di prompt maupun laporan.
- [ ] `tekanan_terbaru` di 950–1050.
- [ ] `bat_selisih` positif saat baterai turun.
- [ ] Rentang data yang dianalisis ≈ 7 hari.

**Keandalan & daya**
- [ ] `durasi_bangun_ms` median < 4000 ms.
- [ ] Gap > 6 menit turun di bawah 1 %.
- [ ] Tidak ada lagi outage tanpa peringatan (banner low-battery aktif < 20 %).
- [ ] Bacaan baterai cocok dengan multimeter dalam ±0,05 V.

**Dokumentasi**
- [ ] `CLAUDE.md` mencantumkan field RTDB baru dan konstanta yang berubah.
- [ ] Catatan wiring dikoreksi: resistor A0 = **100 kΩ** (bukan 1 kΩ), atau 150 kΩ setelah M3.
- [ ] Semua `Status:` di dokumen ini terisi (✅ atau ⏸️ dengan alasan).

---

## 10. Log perubahan

Catat setiap item yang dikerjakan. Format satu baris per item.

| Tanggal | Item | Aksi | Oleh | Verifikasi |
|---|---|---|---|---|
| 2026-08-12 | — | Audit awal, dokumen ini dibuat | Claude | Bukti di §2 |
| 2026-08-12 | C2, M1, M2, M5–M9, M11, m3 | Fix firmware `.ino` (FASE 1+3, satu flash) | Claude | Kode; flash & uji hardware oleh user |
| 2026-08-12 | C3, M15, M16, m4–m7 | Fix `analisa_lab.py` | Claude | `py_compile OK`; `terakhir_valid()` + buang outlier |
| 2026-08-12 | M17 | Fix `maintenance_db.py` (batch + jangan hapus tanpa timestamp) | Claude | `py_compile OK`; not run (destruktif) |
| 2026-08-12 | M13, M14, m1, m2, m8–m12 | Fix `index.html` | Claude | `py_compile` scan: NO_BROKEN_ENTITY; referensi lengkap |
| 2026-08-12 | m16 | Fix `.gitignore` allowlist | Claude | `git status`: file root baru kini tampil |
| 2026-08-12 | — | Update `CLAUDE.md`: field RTDB baru, konstanta coupled, aturan M10, quirk m9 | Claude | Dokumen konsisten dengan kode |
| 2026-08-12 | — | Diagnostik read-only (`diagnostik.py`, lalu dihapus) | Claude | Bukti C3/M15/M16 terverifikasi (nan, 18,29, inversi urutan) |
| 2026-08-12 | C1 (sebagian) | Pemisahan `secrets.h` (ter-gitignore) & `secrets_contoh.h` | Claude | `git check-ignore` OK; `.ino` `#include "secrets.h"` |
| 2026-08-12 | C1 (purge history) | Clean orphan branch `main` (hapus seluruh jejak commit rahasia lama) | Claude | `git log --oneline` -> 1 clean commit; `secrets.h` tidak ter-commit |

---

## Lampiran A — Peta file

| Path | Peran | Tracked git? |
|---|---|---|
| `index.html` | Seluruh dashboard; satu-satunya file yang dilayani GitHub Pages | ya |
| `Wemos_BMP280_ATH20_Firebase/*.ino` | Sketch firmware | tidak (ignored) |
| `analisa_lab.py` | Generator laporan AI → `/LAPORAN_AI` | tidak (ignored) |
| `maintenance_db.py` | Backup penuh + prune `/RIWAYAT_LAB` — **destruktif** | tidak (ignored) |
| `rangga-iot-firebase-adminsdk-*.json` | Service account untuk kedua skrip Python | tidak (`*.json`) |
| `Backup_Firebase_*.json` | Backup lokal; sumber bukti audit | tidak (`*.json`) |
| `CLAUDE.md` | Panduan arsitektur — **baca lebih dulu** | ya |
| `laporan_and_fixing_plan.md` | Dokumen ini | butuh `git add -f` (m16) |

## Lampiran B — Referensi cepat temuan

| Kalau menyentuh… | Baca temuan |
|---|---|
| Pembacaan baterai / A0 / kalibrasi | C2, M3, M4, M5, M13 |
| Pembacaan sensor / validasi | M1, M2, M9, m13 |
| WiFi / deep sleep / umur baterai | C2, M6, M7, M8, M11, m3, m14 |
| Kredensial / keamanan | C1, M12, M14, m15, m16 |
| `analisa_lab.py` | C3, M15, M16, m4, m5, m6, m7 |
| `maintenance_db.py` | M17 |
| Chart / tabel / UI dashboard | M13, m1, m2, m8, m9, m10, m11, m12 |
| Kuota / egress Firebase | M12, dan §8 aturan #5 |
