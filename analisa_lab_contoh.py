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
WAQI_API_KEY = "YOUR_WAQI_API_TOKEN"            # Dapatkan API Token Gratis dari aqicn.org/data-platform/token/
LAT_LAB = -6.2088                               # Koordinat Latitude lokasi perangkat
LON_LAB = 106.8456                              # Koordinat Longitude lokasi perangkat
KOTA_LAB = "Jakarta"                            # Nama Kota / Area

# DAFTAR MODEL AI (Sistem Fallback)
# Skrip akan mencoba dari urutan teratas. Jika gagal, turun ke bawahnya.
# Nama model diverifikasi dengan `ollama list`:
DAFTAR_MODEL_AI = [
    "gemma4:cloud",           # Model Utama (Paling Pintar, Paling Berat — cloud)
    "gpt-oss:120b-cloud",     # Model Cadangan 1 (cloud)
    "nemotron-3-super:cloud", # Model Cadangan 2 (cloud)
    "gemma4:e2b",             # Cadangan lokal (7,2 GB)
    "gemma3:4b"               # Jaring pengaman terakhir (ringan, pasti terpasang)
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
# - Potong berdasarkan WAKTU, bukan jumlah record (limit_to_last hanyalah batas jumlah).
# - Urutkan eksplisit: push key TIDAK dijamin kronologis karena ESP8266 kehilangan jam saat deep sleep.
# - Buang outlier fisik SEBELUM statistik dan sentinel 0.0 V.
print("[2/4] Menarik data 1 minggu terakhir dari Firebase dan menghitung statistik...")

HARI_ANALISIS = 7
BATAS_BAWAH_VCC = 0.5          # sentinel 0.0 V = gagal baca ADC, bukan bacaan nyata
RENTANG_WAJAR = {              # validasi fisik — samakan dengan firmware
    "suhu": (-10, 60),
    "kelembapan": (1, 100),
    "tekanan": (950, 1050),
}

ref = db.reference('/RIWAYAT_LAB')
# Ambil longgar (2× kebutuhan) lalu potong berdasarkan waktu, karena push key TIDAK reliabel
data_firebase = ref.order_by_key().limit_to_last(HARI_ANALISIS * 288 * 2).get()

if not data_firebase:
    print("Error: Tidak ada data di Firebase!")
    exit()

# Mengubah data JSON (Dictionary) menjadi Pandas DataFrame
df = pd.DataFrame.from_dict(data_firebase, orient='index')

# Pastikan semua kolom ada, walau seluruh jendela tidak memuatnya
for kolom in ["suhu", "kelembapan", "tekanan", "tegangan_vcc", "timestamp"]:
    if kolom not in df.columns:
        df[kolom] = pd.NA
    df[kolom] = pd.to_numeric(df[kolom], errors="coerce")

# WAJIB: urutkan eksplisit — push key tidak dijamin kronologis (ESP kehilangan jam saat deep sleep)
df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

# Potong ke jendela waktu sebenarnya
batas_ms = (datetime.datetime.now() - datetime.timedelta(days=HARI_ANALISIS)).timestamp() * 1000
df = df[df["timestamp"] >= batas_ms]

# Buang outlier fisik SEBELUM statistik apa pun
for kolom, (lo, hi) in RENTANG_WAJAR.items():
    df.loc[~df[kolom].between(lo, hi), kolom] = pd.NA
# Eliminasi bacaan sensor glitch (0.0, -50, atau suhu kamar tropis tidak realistis)
df.loc[(df["suhu"] <= 10) | (df["suhu"] == -50), "suhu"] = pd.NA
df.loc[df["tegangan_vcc"] < BATAS_BAWAH_VCC, "tegangan_vcc"] = pd.NA

if df.empty:
    print(f"Error: Tidak ada data valid dalam {HARI_ANALISIS} hari terakhir!")
    exit()


def fmt(nilai, satuan="", desimal=1):
    """Format angka untuk prompt; eksplisit kalau data tidak ada."""
    if nilai is None or pd.isna(nilai):
        return "DATA TIDAK TERSEDIA"
    return f"{nilai:.{desimal}f} {satuan}".strip()


def terakhir_valid(kolom):
    """Nilai valid terakhir secara kronologis, bukan baris terakhir."""
    seri = df[kolom].dropna()
    return seri.iloc[-1] if len(seri) else float("nan")


def pertama_valid(kolom):
    seri = df[kolom].dropna()
    return seri.iloc[0] if len(seri) else float("nan")


# PANDAS SEBAGAI KALKULATOR: Menghitung statistik ringkas
suhu_rata2      = df["suhu"].mean()
suhu_maks       = df["suhu"].max()
suhu_min        = df["suhu"].min()      # outlier sudah dibuang
hum_rata2       = df["kelembapan"].mean()
tekanan_rata2   = df["tekanan"].mean()

# Mengambil data kamar kondisi SAAT INI (Data pada baris valid kronologis terakhir)
suhu_terbaru    = terakhir_valid("suhu")
hum_terbaru     = terakhir_valid("kelembapan")
tekanan_terbaru = terakhir_valid("tekanan")

# Menghitung performa baterai selama 1 minggu (sentinel 0.0 sudah dibuang di atas)
bat_awal    = pertama_valid("tegangan_vcc")
bat_akhir   = terakhir_valid("tegangan_vcc")
bat_selisih = bat_awal - bat_akhir if pd.notna(bat_awal) and pd.notna(bat_akhir) else float("nan")

# Menghitung Tren Tekanan Udara (Perubahan dalam 3 Jam Terakhir):
# df sudah terurut, jadi head/tail kini bermakna; outlier sudah dibuang sebelum mean.
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

# 4A. Ambil Data Cuaca OpenWeather (Semua parameter ini GRATIS)
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

# 4B. Ambil Data Siklus Matahari Sunrise-Sunset (Gratis, Tanpa API Key)
wib = datetime.timezone(datetime.timedelta(hours=7))
waktu_sekarang_dt = datetime.datetime.now(wib)
waktu_sekarang_str = waktu_sekarang_dt.strftime('%H:%M WIB, %d %B %Y')
status_hari = "Siang Hari" if 6 <= waktu_sekarang_dt.hour < 18 else "Malam Hari"

try:
    url_sun = f"https://api.sunrise-sunset.org/json?lat={LAT_LAB}&lng={LON_LAB}&formatted=0"
    respon_sun = requests.get(url_sun, timeout=10).json()
    if respon_sun['status'] == 'OK':
        # Format dari API adalah UTC. Konversi ke waktu lokal (WIB / UTC+7)
        sr_dt = datetime.datetime.fromisoformat(respon_sun['results']['sunrise'].replace('Z', '+00:00')).astimezone(wib)
        ss_dt = datetime.datetime.fromisoformat(respon_sun['results']['sunset'].replace('Z', '+00:00')).astimezone(wib)
        
        matahari['sunrise'] = sr_dt.strftime('%H:%M WIB')
        matahari['sunset'] = ss_dt.strftime('%H:%M WIB')
        
        dl_sec = int(respon_sun['results']['day_length'])
        matahari['day_length'] = f"{dl_sec // 3600} jam {(dl_sec % 3600) // 60} menit"

        is_siang = sr_dt <= waktu_sekarang_dt <= ss_dt
        status_hari = "Siang Hari (Matahari Aktif)" if is_siang else "Malam Hari (Pasca Matahari Terbenam)"
except Exception as e:
    print(f"Gagal mengambil data Sunrise-Sunset: {e}")

# 4C. Ambil Kualitas Udara (AQI) dari WAQI
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
- Lokasi Referensi: {KOTA_LAB} (Karakteristik iklim tropis. Suhu rata-rata siang: 31 °C - 34 °C, Suhu rata-rata malam: 24 °C - 26 °C, Kelembapan Relatif / RH standar: 75% - 90%).
- Kondisi Ruangan: Tidak dilengkapi sistem pendingin mekanis (AC), mengandalkan ventilasi pasif dan sirkulasi udara standar (kipas angin).
- Arsitektur Perangkat IoT: Wemos D1 Mini (ESP8266) yang beroperasi menggunakan siklus Deep Sleep dengan interval 5 menit.
- Konfigurasi Sensor Internal: AHT20 (Suhu dan Kelembapan Relatif) serta BMP280 (Tekanan Barometrik dan Suhu).

DATA OBSERVASI LINGKUNGAN SAAT INI (Real-time Apple-to-Apple):
- Waktu Observasi Data: {waktu_sekarang_str} ({status_hari})
[ATURAN ANALISIS WAKTU]: Sesuaikan analisis termal dengan status waktu observasi di atas secara ketat. Jika Malam Hari, JANGAN menyatakan ada radiasi matahari aktif, melainkan analisis pelepasan panas malam hari (thermal lag/heat release).

Parameter Kamar:
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

DATA TREN KAMAR & IOT (Profil {HARI_ANALISIS} Hari Terakhir — {JUMLAH_RECORD} record teranalisis):
- Suhu Rata-rata: {fmt(suhu_rata2, '°C')} (Puncak: {fmt(suhu_maks, '°C')}, Terendah: {fmt(suhu_min, '°C')})
- Kelembapan Rata-rata: {fmt(hum_rata2, '%')}
- Tekanan Rata-rata: {fmt(tekanan_rata2, 'hPa')}
- Penurunan Baterai Alat: {fmt(bat_selisih, 'V', 2)} / {HARI_ANALISIS} hari (Dari {fmt(bat_awal, 'V', 2)} ke {fmt(bat_akhir, 'V', 2)})

INSTRUKSI ANALISIS DAN PELAPORAN:
Berikan laporan teknis yang terstruktur, objektif, dan berbasis metrik. Gunakan bahasa Indonesia yang baku dan profesional. Hindari penggunaan gaya bahasa percakapan atau emoji. Laporan harus mencakup 3 (tiga) pilar analisis berikut:
1. Analisis Termal dan Risiko Lingkungan: 
   Evaluasi perbandingan kondisi kamar SAAT INI dengan suhu dan "feels like" luar. Kaitkan posisi matahari dan tutupan awan terhadap paparan panas pada ruangan. Tinjau juga Tren Mingguan untuk potensi *heat trap* atau risiko proliferasi jamur. Formulasikan rekomendasi mitigasi udara yang realistis (tanpa AC).
2. Tinjauan Meteorologi Mikro dan Kualitas Udara: 
   Gunakan tekanan barometrik kamar dan luar ruangan beserta trennya, ditambah kecepatan/arah angin, untuk memproyeksikan cuaca lokal jangka pendek. Selain itu, berikan keputusan tegas mengenai Kualitas Udara (AQI) luar ruangan: apakah saat ini aman untuk membuka jendela/ventilasi lebar-lebar, atau justru berisiko memasukkan polutan (PM2.5/PM10) ke dalam ruangan.
3. Diagnostik Kinerja Perangkat Keras dan Manajemen Daya: 
   Evaluasi efisiensi konsumsi daya ESP8266 berdasarkan profil interval *deep sleep* 5 menit dan delta penurunan tegangan. 
   [PROTOKOL PENANGANAN ANOMALI DAYA]: Apabila data tegangan baterai bernilai 0.00 V, HENTIKAN segala bentuk estimasi efisiensi baterai. Segera terbitkan laporan diagnostik yang mengindikasikan kemungkinan kegagalan fisik (pin ADC A0 tidak terhubung), kesalahan kalkulasi pada rangkaian *voltage divider*, atau anomali pada rutinitas inisialisasi pasca-*deep sleep*.

FORMAT KELUARAN:
- Judul Laporan: Langsung mulai dengan `# Evaluasi Iklim Mikro & Telemetri IoT` (DILARANG menggunakan pembuka memo seperti "Kepada:", "Perihal:").
- Ringkasan Eksekutif (Wajib di awal): Tepat setelah judul utama, buat kotak rangkuman cepat menggunakan format blockquote berikut:
  > ### Ringkasan Eksekutif (Status Cepat)
  > - **Status Termal**: [AMAN / WASPADA / KRITIS] — [Rangkuman 1 kalimat kondisi termal kamar vs luar]
  > - **Sirkulasi & Kualitas Udara**: [VENTILASI AMAN / TUTUP VENTILASI] — [Rangkuman 1 kalimat tentang AQI & angin]
  > - **Kesehatan Perangkat IoT**: [NORMAL / ANOMALI] — [Rangkuman 1 kalimat tentang sensor dan daya baterai]
- Gunakan sub-heading Markdown level 3 (`###`) untuk setiap sub-bagian (contoh: `### 1. Analisis Termal dan Risiko Lingkungan`, `### 2. Tinjauan Meteorologi Mikro dan Kualitas Udara`, `### 3. Diagnostik Kinerja Perangkat Keras dan Manajemen Daya`, serta `### Rekomendasi Mitigasi`).
- Khusus untuk bagian Rekomendasi Mitigasi, gunakan format blockquote (`> - Rekomendasi...`) agar tampil sebagai poin aksi prioritas.
- ATURAN ERGONOMI BACA (ANTI-BOLD FATIGUE): Gunakan teks tebal (**bold**) SECARA SANGAT SELEKTIF dan HEMAT. HANYA tebalkan label status atau putusan utama (misal: **NORMAL**, **WASPADA**, **ANOMALI**). DILARANG menebalkan setiap angka metrik, satuan, atau variabel biasa di dalam kalimat (tulis biasa: 'suhu 30.5 °C', 'kelembapan 82%', 'tegangan 0.00 V', BUKAN ditebalkan). Kalimat harus mengalir halus dan tidak melelahkan mata.
- DILARANG menggunakan notasi LaTeX atau tanda dollar ($...$, \\geq, \\downarrow, \\Delta). Gunakan karakter teks atau simbol Unicode standar langsung (contoh: ≥, ↓, ΔV, %).
"""

print("[4/4] Mengirim data ke Ollama (Sistem Fallback AI aktif)...")

# Memaksa koneksi menggunakan IPv4 lokal secara eksplisit
ollama_client = Client(host='http://127.0.0.1:11434')

hasil_analisa = ""
for model_name in DAFTAR_MODEL_AI:
    print(f" -> Mencoba menggunakan model: '{model_name}'...")
    try:
        # Jika model tidak ada atau RAM habis, fungsi ini akan menghasilkan Error (Exception)
        response = ollama_client.chat(model=model_name, messages=[
            {'role': 'user', 'content': prompt_untuk_ai}
        ])
        hasil_analisa = response['message']['content']
        print(f"\n[SUKSES] Model '{model_name}' berhasil memproses data!\n")
        break # Berhenti dari loop karena model sukses terpanggil
    except Exception as e:
        print(f"    [GAGAL] '{model_name}' tidak dapat digunakan. Alasan: {e}")
        print("    Beralih ke model cadangan berikutnya...\n")

if not hasil_analisa:
    print("MAAF, SEMUA MODEL AI GAGAL DIPROSES.")
else:
    print("================ LAPORAN AI ================")
    print(hasil_analisa)
    print("============================================")

    # Simpan hasil ke dalam file teks (Otomatis)
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
