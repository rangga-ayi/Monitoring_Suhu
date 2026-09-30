import firebase_admin
from firebase_admin import credentials, db
import datetime
import json
import time

# ==========================================
# 1. KONFIGURASI (TEMPLATE / CONTOH)
# ==========================================
# Ganti dengan nama file Service Account JSON Anda
FIREBASE_KEY_PATH = "firebase_adminsdk_contoh.json"
# Ganti dengan URL Realtime Database Firebase Anda
FIREBASE_DB_URL = "https://your-project-id-default-rtdb.asia-southeast1.firebasedatabase.app"

# Rentang wajar — samakan dengan firmware dan analisa_lab.py
RENTANG_WAJAR = {"suhu": (-10, 60), "kelembapan": (1, 100), "tekanan": (950, 1050)}

# ==========================================
# 2. INISIALISASI FIREBASE
# ==========================================
print("[1/4] Menghubungkan ke Firebase...")
if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_KEY_PATH)
    firebase_admin.initialize_app(cred, {
        'databaseURL': FIREBASE_DB_URL
    })

ref = db.reference('/RIWAYAT_LAB')
print("[2/4] Mengunduh seluruh data dari Firebase (harap tunggu)...")
all_data = ref.get()

if not all_data:
    print("Database kosong atau gagal diakses.")
    exit()

# ==========================================
# 3. MELAKUKAN BACKUP (CADANGAN LOKAL)
# ==========================================
waktu_sekarang = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
backup_filename = f"Backup_Firebase_{waktu_sekarang}.json"

with open(backup_filename, "w", encoding="utf-8") as f:
    json.dump(all_data, f, indent=4)
print(f"[3/4] SUKSES: Data berhasil dicadangkan ke file '{backup_filename}'")

# ==========================================
# 4. MENGIDENTIFIKASI DATA ERROR & KEDALUWARSA
# ==========================================
keys_to_delete = []
keys_tanpa_timestamp = []
jumlah_error = 0
jumlah_usang = 0

# Hitung batas waktu 6 bulan yang lalu dalam milidetik (Epoch)
batas_waktu_ms = (time.time() - (180 * 24 * 60 * 60)) * 1000


def sensor_valid_atau_absen(nama, data):
    """True jika field absen (diizinkan untuk data sensor parsial) atau nilainya dalam rentang wajar."""
    nilai = data.get(nama)
    if nilai is None:
        return True
    try:
        val = float(nilai)
    except (ValueError, TypeError):
        return False
    if nama == "suhu" and (val <= 10 or val == -50):
        return False
    lo, hi = RENTANG_WAJAR[nama]
    return lo <= val <= hi


for push_key, data in all_data.items():
    if not isinstance(data, dict):        # child non-object, masukkan ke antrean hapus
        keys_to_delete.append(push_key)
        jumlah_error += 1
        continue

    timestamp = data.get("timestamp")
    if timestamp is None:
        keys_tanpa_timestamp.append(push_key)   # JANGAN hapus otomatis — laporkan untuk review manual
        continue

    # Sensor mati total jika semua field sensor absen atau firmware mencatat kegagalan total
    semua_sensor_kosong = (
        data.get("suhu") is None
        and data.get("kelembapan") is None
        and data.get("tekanan") is None
    )
    status_sensor_gagal = data.get("status_sensor") == "semua_gagal"

    # Periksa outlier / glitch pada sensor yang aktif mengirim data
    sensor_rusak = (
        not sensor_valid_atau_absen("suhu", data)
        or not sensor_valid_atau_absen("kelembapan", data)
        or not sensor_valid_atau_absen("tekanan", data)
    )

    is_error = semua_sensor_kosong or status_sensor_gagal or sensor_rusak
    is_old = timestamp < batas_waktu_ms

    if is_error:
        jumlah_error += 1
    elif is_old:
        jumlah_usang += 1
    if is_error or is_old:
        keys_to_delete.append(push_key)

# ==========================================
# 5. EKSEKUSI PENGHAPUSAN
# ==========================================
print("\n--- HASIL ANALISIS DATABASE ---")
print(f"Total data di Firebase : {len(all_data)} baris")
print(f"Data Sensor Error      : {jumlah_error} baris")
print(f"Data Lebih dari 6 Bulan: {jumlah_usang} baris")
print("-------------------------------")

if keys_tanpa_timestamp:
    print(f"PERINGATAN: {len(keys_tanpa_timestamp)} record tanpa timestamp — "
          f"TIDAK dihapus, perlu review manual.")

if len(keys_to_delete) > 0:
    konfirmasi = input(f"Apakah Anda yakin ingin menghapus {len(keys_to_delete)} data tersebut dari Firebase? (y/n): ")
    if konfirmasi.lower() == 'y':
        print("[4/4] Menghapus data dari Firebase (batch)...")
        UKURAN_BATCH = 500
        for i in range(0, len(keys_to_delete), UKURAN_BATCH):
            potongan = keys_to_delete[i:i + UKURAN_BATCH]
            ref.update({k: None for k in potongan})    # satu request per batch, bukan per record
            print(f"  {min(i + UKURAN_BATCH, len(keys_to_delete))}/{len(keys_to_delete)}")
        print("PEMBERSIHAN SELESAI! Database kini sudah bersih.")
    else:
        print("Penghapusan dibatalkan. Data tetap aman.")
else:
    print("Tidak ditemukan data error maupun usang. Database sudah bersih!")
