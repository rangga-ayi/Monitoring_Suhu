import firebase_admin
from firebase_admin import credentials, db
import pandas as pd
import requests
import datetime
from ollama import Client

# ==========================================
# 1. KONFIGURASI (TEMPLATE / CONTOH)
# ==========================================
# Ganti dengan nama file kredensial Service Account JSON Anda
FIREBASE_KEY_PATH = "firebase_adminsdk_contoh.json"
# Ganti dengan URL Firebase Realtime Database Anda
FIREBASE_DB_URL = "https://your-project-id-default-rtdb.asia-southeast1.firebasedatabase.app"

# Kredensial API Eksternal
OWM_API_KEY = "YOUR_OPENWEATHERMAP_API_KEY"      # Dapatkan dari https://openweathermap.org/api
WAQI_API_KEY = "YOUR_WAQI_API_TOKEN"            # Dapatkan dari https://aqicn.org/data-platform/token/
LAT_LAB = -6.2088                               # Koordinat Latitude lokasi perangkat
LON_LAB = 106.8456                              # Koordinat Longitude lokasi perangkat
KOTA_LAB = "Jakarta"                            # Nama Kota / Area

# DAFTAR MODEL AI (Sistem Fallback)
# Skrip akan mencoba dari urutan teratas. Jika gagal, turun ke model berikutnya.
DAFTAR_MODEL_AI = [
    "gemma4:31b-cloud",       # Model Utama (Cloud / Bobot Besar)
    "gpt-oss:120b-cloud",     # Model Cadangan 1
    "nemotron-3-super:cloud", # Model Cadangan 2
    "gemma4:e2b",             # Cadangan lokal
    "llama3:8b",              # Model lokal standar
    "gemma3:4b"               # Jaring pengaman terakhir (ringan)
]

# ==========================================
# 2. INISIALISASI FIREBASE
# ==========================================
print("[1/4] Menghubungkan ke Firebase...")
if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_KEY_PATH)
    firebase_admin.initialize_app(cred, {
        'databaseURL': FIREBASE_DB_URL
    })

# ==========================================
# 3. MENARIK & MENGHITUNG DATA (PANDAS)
# ==========================================
print("[2/4] Menarik data 1 minggu terakhir dari Firebase dan menghitung statistik...")

HARI_ANALISIS = 7
BATAS_BAWAH_VCC = 0.5          # sentinel 0.0 V = gagal baca ADC
RENTANG_WAJAR = {              # validasi fisik
    "suhu": (-10, 60),
    "kelembapan": (1, 100),
    "tekanan": (950, 1050),
}

ref = db.reference('/RIWAYAT_LAB')
data_firebase = ref.order_by_key().limit_to_last(HARI_ANALISIS * 288 * 2).get()

if not data_firebase:
    print("Error: Tidak ada data di Firebase!")
    exit()

df = pd.DataFrame.from_dict(data_firebase, orient='index')

for kolom in ["suhu", "kelembapan", "tekanan", "tegangan_vcc", "timestamp"]:
    if kolom not in df.columns:
        df[kolom] = pd.NA
    df[kolom] = pd.to_numeric(df[kolom], errors="coerce")

df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

batas_ms = (datetime.datetime.now() - datetime.timedelta(days=HARI_ANALISIS)).timestamp() * 1000
df = df[df["timestamp"] >= batas_ms]

for kolom, (lo, hi) in RENTANG_WAJAR.items():
    df.loc[~df[kolom].between(lo, hi), kolom] = pd.NA
# Eliminasi bacaan sensor glitch (0.0, -50, atau suhu kamar tropis tidak realistis)
df.loc[(df["suhu"] <= 10) | (df["suhu"] == -50), "suhu"] = pd.NA
df.loc[df["tegangan_vcc"] < BATAS_BAWAH_VCC, "tegangan_vcc"] = pd.NA

if df.empty:
    print(f"Error: Tidak ada data valid dalam {HARI_ANALISIS} hari terakhir!")
    exit()


def fmt(nilai, satuan="", desimal=1):
    if nilai is None or pd.isna(nilai):
        return "DATA TIDAK TERSEDIA"
    return f"{nilai:.{desimal}f} {satuan}".strip()


def terakhir_valid(kolom):
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

bat_awal    = pertama_valid("tegangan_vcc")
bat_akhir   = terakhir_valid("tegangan_vcc")
bat_selisih = bat_awal - bat_akhir if pd.notna(bat_awal) and pd.notna(bat_akhir) else float("nan")

tren_tekanan = 0.0
df_tekanan = df.dropna(subset=["tekanan"])
if len(df_tekanan) >= 6:
    batas_3jam = df_tekanan["timestamp"].iloc[-1] - (3 * 60 * 60 * 1000)
    df_3jam = df_tekanan[df_tekanan["timestamp"] >= batas_3jam]
    if len(df_3jam) >= 6:
        tren_tekanan = df_3jam["tekanan"].tail(3).mean() - df_3jam["tekanan"].head(3).mean()

status_tren = ("Naik (Membaik/Cerah)"   if tren_tekanan >  0.5 else
               "Turun (Memburuk/Hujan)" if tren_tekanan < -0.5 else "Stabil")

JUMLAH_RECORD = len(df)

# ==========================================
# 4. MENARIK DATA CUACA DARI API (OPENWEATHER)
# ==========================================
print(f"[3/4] Mengambil metrik meteorologi dan lingkungan untuk {KOTA_LAB}...")
cuaca_luar = {
    "suhu": None, "kelembapan": None, "feels_like": None, 
    "wind_speed": None, "wind_deg": None, "clouds": None, "pressure": None
}
matahari = {"sunrise": "-", "sunset": "-", "day_length": "-"}
aqi_luar = {"aqi": "-", "dominentpol": "-", "keterangan": "Data WAQI tidak diaktifkan"}

if OWM_API_KEY != "YOUR_OPENWEATHERMAP_API_KEY":
    try:
        url_owm = f"http://api.openweathermap.org/data/2.5/weather?lat={LAT_LAB}&lon={LON_LAB}&appid={OWM_API_KEY}&units=metric"
        respon_cuaca = requests.get(url_owm, timeout=10).json()
        cuaca_luar['suhu'] = respon_cuaca['main']['temp']
        cuaca_luar['kelembapan'] = respon_cuaca['main']['humidity']
        cuaca_luar['feels_like'] = respon_cuaca['main']['feels_like']
        cuaca_luar['pressure'] = respon_cuaca['main']['pressure']
        cuaca_luar['wind_speed'] = respon_cuaca['wind']['speed']
        cuaca_luar['wind_deg'] = respon_cuaca['wind']['deg']
        cuaca_luar['clouds'] = respon_cuaca['clouds']['all']
    except Exception as e:
        print(f"Gagal mengambil data OWM: {e}")

try:
    url_sun = f"https://api.sunrise-sunset.org/json?lat={LAT_LAB}&lng={LON_LAB}&formatted=0"
    respon_sun = requests.get(url_sun, timeout=10).json()
    if respon_sun['status'] == 'OK':
        wib = datetime.timezone(datetime.timedelta(hours=7))
        sr_dt = datetime.datetime.fromisoformat(respon_sun['results']['sunrise'].replace('Z', '+00:00')).astimezone(wib)
        ss_dt = datetime.datetime.fromisoformat(respon_sun['results']['sunset'].replace('Z', '+00:00')).astimezone(wib)
        
        matahari['sunrise'] = sr_dt.strftime('%H:%M WIB')
        matahari['sunset'] = ss_dt.strftime('%H:%M WIB')
        
        dl_sec = int(respon_sun['results']['day_length'])
        matahari['day_length'] = f"{dl_sec // 3600} jam {(dl_sec % 3600) // 60} menit"
except Exception as e:
    print(f"Gagal mengambil data Sunrise-Sunset: {e}")

if WAQI_API_KEY != "YOUR_WAQI_API_TOKEN":
    try:
        url_waqi = f"https://api.waqi.info/feed/geo:{LAT_LAB};{LON_LAB}/?token={WAQI_API_KEY}"
        respon_waqi = requests.get(url_waqi, timeout=10).json()
        if respon_waqi['status'] == 'ok':
            val_aqi = respon_waqi['data']['aqi']
            aqi_luar['aqi'] = val_aqi
            aqi_luar['dominentpol'] = respon_waqi['data'].get('dominentpol', '-')
            if val_aqi <= 50: aqi_luar['keterangan'] = "Baik"
            elif val_aqi <= 100: aqi_luar['keterangan'] = "Sedang"
            elif val_aqi <= 150: aqi_luar['keterangan'] = "Tidak Sehat bagi Kelompok Sensitif"
            elif val_aqi <= 200: aqi_luar['keterangan'] = "Tidak Sehat"
            else: aqi_luar['keterangan'] = "Sangat Tidak Sehat / Berbahaya"
    except Exception as e:
        print(f"Gagal mengambil data WAQI: {e}")

# ==========================================
# 5. MEMBUAT PROMPT & MENJALANKAN AI
# ==========================================
prompt_untuk_ai = f"""
Anda adalah seorang Insinyur IoT dan Analis Data Lingkungan. Tugas Anda adalah memberikan evaluasi teknis yang komprehensif mengenai komparasi iklim mikro dalam ruangan dengan data meteorologi luar ruangan, serta mengevaluasi efisiensi kinerja perangkat keras IoT.

KONTEKS LINGKUNGAN DAN PERANGKAT:
- Lokasi Referensi: {KOTA_LAB}
- Kondisi Ruangan: Ventilasi pasif dan sirkulasi udara alami.
- Arsitektur Perangkat IoT: Wemos D1 Mini (ESP8266) yang beroperasi menggunakan siklus Deep Sleep dengan interval 5 menit.
- Konfigurasi Sensor Internal: AHT20 (Suhu dan Kelembapan Relatif) serta BMP280 (Tekanan Barometrik dan Suhu).

DATA OBSERVASI LINGKUNGAN SAAT INI:
Parameter Ruangan:
- Suhu Kamar Saat Ini: {fmt(suhu_terbaru, '°C')}
- Kelembapan Kamar Saat Ini: {fmt(hum_terbaru, '%')}
- Tekanan Udara Kamar Saat Ini: {fmt(tekanan_terbaru, 'hPa')} (Perubahan 3 jam terakhir: {fmt(tren_tekanan, 'hPa', 2)} -> Tren {status_tren})
Kondisi Eksternal {KOTA_LAB} Terkini:
- Suhu Eksternal (Luar): {fmt(cuaca_luar['suhu'], '°C')} (Feels like: {fmt(cuaca_luar['feels_like'], '°C')})
- Kelembapan Eksternal (Luar): {fmt(cuaca_luar['kelembapan'], '%')}
- Tekanan Eksternal (Luar): {fmt(cuaca_luar['pressure'], 'hPa')}
- Angin Luar: Kecepatan {fmt(cuaca_luar['wind_speed'], 'm/s')}, Arah {fmt(cuaca_luar['wind_deg'], '°', 0)}
- Tutupan Awan: {fmt(cuaca_luar['clouds'], '%', 0)}
- Siklus Sinar Matahari: Terbit {matahari['sunrise']}, Terbenam {matahari['sunset']} (Total siang: {matahari['day_length']})
- Kualitas Udara Luar (AQI): {aqi_luar['aqi']} ({aqi_luar['keterangan']}) - Polutan utama: {aqi_luar['dominentpol']}

DATA TREN RUANGAN & IOT (Profil {HARI_ANALISIS} Hari Terakhir — {JUMLAH_RECORD} record teranalisis):
- Suhu Rata-rata: {fmt(suhu_rata2, '°C')} (Puncak: {fmt(suhu_maks, '°C')}, Terendah: {fmt(suhu_min, '°C')})
- Kelembapan Rata-rata: {fmt(hum_rata2, '%')}
- Tekanan Rata-rata: {fmt(tekanan_rata2, 'hPa')}
- Penurunan Baterai Alat: {fmt(bat_selisih, 'V', 2)} / {HARI_ANALISIS} hari (Dari {fmt(bat_awal, 'V', 2)} ke {fmt(bat_akhir, 'V', 2)})

INSTRUKSI ANALISIS DAN PELAPORAN:
Berikan laporan teknis yang terstruktur, objektif, dan berbasis metrik:
1. Analisis Termal dan Risiko Lingkungan
2. Tinjauan Meteorologi Mikro dan Kualitas Udara
3. Diagnostik Kinerja Perangkat Keras dan Manajemen Daya (Jika tegangan 0.00 V terbitkan indikasi kegagalan ADC/divider)

FORMAT KELUARAN:
- Judul Laporan: Langsung mulai dengan `# Evaluasi Iklim Mikro & Telemetri IoT` (DILARANG menggunakan pembuka memo seperti "Kepada:", "Perihal:").
- Ringkasan Eksekutif (Wajib di awal): Tepat setelah judul utama, buat kotak rangkuman cepat menggunakan format blockquote berikut:
  > ### Ringkasan Eksekutif (Status Cepat)
  > - **Status Termal**: [AMAN / WASPADA / KRITIS] — [Rangkuman 1 kalimat kondisi termal kamar vs luar]
  > - **Sirkulasi & Kualitas Udara**: [VENTILASI AMAN / TUTUP VENTILASI] — [Rangkuman 1 kalimat tentang AQI & angin]
  > - **Kesehatan Perangkat IoT**: [NORMAL / ANOMALI] — [Rangkuman 1 kalimat tentang sensor dan daya baterai]
- Gunakan sub-heading Markdown level 3 (`###`) untuk setiap sub-bagian.
- Khusus untuk bagian Rekomendasi Mitigasi, gunakan format blockquote (`> - Rekomendasi...`).
- ATURAN ERGONOMI BACA (ANTI-BOLD FATIGUE): Gunakan teks tebal (**bold**) SECARA SANGAT SELEKTIF dan HEMAT. HANYA tebalkan label status atau putusan utama (misal: **NORMAL**, **WASPADA**, **ANOMALI**). DILARANG menebalkan setiap angka metrik, satuan, atau variabel biasa di dalam kalimat (tulis biasa: 'suhu 30.5 °C', 'kelembapan 82%', 'tegangan 0.00 V', BUKAN ditebalkan). Kalimat harus mengalir halus dan tidak melelahkan mata.
- DILARANG menggunakan notasi LaTeX atau tanda dollar ($...$, \\geq, \\downarrow, \\Delta). Gunakan karakter Unicode standar langsung (contoh: ≥, ↓, ΔV, %).
"""

print("[4/4] Mengirim data ke Ollama (Sistem Fallback AI aktif)...")

ollama_client = Client(host='http://127.0.0.1:11434')

hasil_analisa = ""
for model_name in DAFTAR_MODEL_AI:
    print(f" -> Mencoba menggunakan model: '{model_name}'...")
    try:
        response = ollama_client.chat(model=model_name, messages=[
            {'role': 'user', 'content': prompt_untuk_ai}
        ])
        hasil_analisa = response['message']['content']
        print(f"\n[SUKSES] Model '{model_name}' berhasil memproses data!\n")
        break
    except Exception as e:
        print(f"    [GAGAL] '{model_name}' tidak dapat digunakan. Alasan: {e}")
        print("    Beralih ke model cadangan berikutnya...\n")

if not hasil_analisa:
    print("MAAF, SEMUA MODEL AI GAGAL DIPROSES.")
else:
    print("================ LAPORAN AI ================")
    print(hasil_analisa)
    print("============================================")

    waktu_sekarang = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    nama_file = f"Laporan_AI_{waktu_sekarang}.txt"
    with open(nama_file, "w", encoding="utf-8") as file:
        file.write(f"Laporan Analisa Kamar\nTanggal: {datetime.datetime.now().strftime('%d %B %Y %H:%M')}\n\n")
        file.write(hasil_analisa)
    print(f"\n[INFO] Laporan berhasil disimpan ke file: {nama_file}")

    print("[5/5] Mengunggah laporan ke Firebase...")
    try:
        ref_ai = db.reference('/LAPORAN_AI')
        ref_ai.set({
            'timestamp': datetime.datetime.now().timestamp() * 1000,
            'laporan_text': hasil_analisa
        })
        print("[SUKSES] Laporan AI berhasil diunggah ke Firebase!")
    except Exception as e:
        print(f"[GAGAL] Terjadi kesalahan saat mengunggah ke Firebase: {e}")
