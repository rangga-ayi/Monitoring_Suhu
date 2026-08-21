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

# Rentang wajar sensor
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

BATAS_HARI_SIMPAN = 30
batas_waktu_ms = (datetime.datetime.now() - datetime.timedelta(days=BATAS_HARI_SIMPAN)).timestamp() * 1000

for key, val in all_data.items():
    if not isinstance(val, dict):
        keys_to_delete.append(key)
        jumlah_error += 1
        continue

    is_error = False
    for sensor, (min_val, max_val) in RENTANG_WAJAR.items():
        if sensor in val:
            try:
                v = float(val[sensor])
                if v < min_val or v > max_val:
                    is_error = True
                    break
            except (ValueError, TypeError):
                is_error = True
                break

    if is_error:
        keys_to_delete.append(key)
        jumlah_error += 1
        continue

    if "timestamp" in val:
        try:
            ts = float(val["timestamp"])
            if ts < batas_waktu_ms:
                keys_to_delete.append(key)
                jumlah_usang += 1
        except (ValueError, TypeError):
            keys_tanpa_timestamp.append(key)
    else:
        keys_tanpa_timestamp.append(key)

print(f"Total data ditemukan: {len(all_data)}")
print(f"- Data error/rusak : {jumlah_error}")
print(f"- Data usang (>30h) : {jumlah_usang}")
print(f"- Data tanpa waktu : {len(keys_tanpa_timestamp)}")
print(f"Total antrean hapus : {len(keys_to_delete)}")

if not keys_to_delete:
    print("Database sudah bersih, tidak ada data yang perlu dihapus.")
    exit()

# ==========================================
# 5. KONFIRMASI PENGHAPUSAN
# ==========================================
konfirmasi = input("\nApakah Anda yakin ingin MENGHAPUS data tersebut dari Firebase? (y/n): ")
if konfirmasi.lower() == 'y':
    print("Menghapus data di Firebase...")
    update_dict = {key: None for key in keys_to_delete}
    ref.update(update_dict)
    print("Pembersihan database selesai!")
else:
    print("Operasi dibatalkan. Data tidak dihapus.")
