#include <ESP8266WiFi.h>
#include <WiFiManager.h>
#include <Wire.h>
#include <Adafruit_AHTX0.h>
#include <Adafruit_BMP280.h>
#include <Firebase_ESP_Client.h>

// --- PERBAIKAN UTAMA: WAJIB ADA ADDONS INI ---
// Tanpa ini, Legacy Token tidak bisa diproses oleh library
#include <addons/TokenHelper.h> 
#include <addons/RTDBHelper.h> 

// --- KREDENSIAL RAHASIA (DIAMBIL DARI secrets.h - TER-GITIGNORE) ---
#include "secrets.h"

// --- PIN I2C (harus eksplisit untuk bus recovery) ---
const int PIN_SDA = D2;   // GPIO 4
const int PIN_SCL = D1;   // GPIO 5

// --- PIN JUMPER KONFIGURASI WIFI ---
// Hubungkan pin ini ke GND dengan kabel jumper untuk masuk mode Captive Portal (Ganti WiFi)
const int PIN_CONFIG = D5;

// --- SETTING DEEP SLEEP ---
const int SLEEP_SECONDS = 300; 

// --- KONFIGURASI KALIBRASI SENSOR ---
const float ELEVASI_PERANGKAT = 8.0;      
const float OFFSET_TEKANAN = 1.53;
// Rumus Kalibrasi: Multiplier Lama * (Tegangan Multitester / Tegangan Web)
const float MULTIPLIER_BATERAI = 0.004038; 

// --- RENTANG WAJAR SENSOR (samakan dengan analisa_lab.py RENTANG_WAJAR) ---
const float SUHU_MIN = -10.0, SUHU_MAKS = 60.0;
const float HUM_MIN  =   1.0, HUM_MAKS  = 100.0;
const float TEK_MIN  = 950.0, TEK_MAKS  = 1050.0;

// --- AMBANG PROTEKSI DAYA (C2) ---
const float BATAS_KRITIS     = 3.40;   // di bawah ini LDO mulai drop out
const float BATAS_PERINGATAN = 3.60;
const uint32_t TIDUR_HEMAT_S = 3600;   // 1 jam saat baterai kritis

// --- OBJEK ---
Adafruit_AHTX0 aht;
Adafruit_BMP280 bmp;
FirebaseData fbdo;
FirebaseAuth auth;
FirebaseConfig config;

// --- SAKLAR MODE ---
const bool DEBUG_MODE = false; // Ubah ke 'true' untuk mode debug (tetap bangun, push tiap 10 detik)

bool ahtStatus = true;
bool bmpStatus = true;

// --- GLOBAL (M5: baca baterai paling awal, sebelum WiFi menyala) ---
float teganganBaterai = 0.0;
int   rawBaterai      = 0;

/** Baca tegangan baterai. WAJIB dipanggil sebelum WiFi menyala (M5). */
float bacaBaterai(int &rawKeluar) {
  const int JUMLAH_SAMPEL_ADC = 32;
  const int AMBANG_ADC_GAGAL  = 50;   // di bawah ini = A0 tak terhubung
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
  rawKeluar = sampel[JUMLAH_SAMPEL_ADC / 2];   // median (tahan terhadap spike ADC)

  if (rawKeluar < AMBANG_ADC_GAGAL) return 0.0;   // sentinel: gagal baca ADC
  return MULTIPLIER_BATERAI * rawKeluar;
}

/**
 * Pemulihan bus I2C yang terkunci (M12).
 *
 * Saat deep sleep, GPIO ESP8266 mengambang sementara sensor tetap bertenaga.
 * Jika transaksi I2C terpotong, slave bisa menahan SDA LOW tanpa batas waktu.
 * Wire.begin() bawaan ESP8266 TIDAK melakukan bus clear.
 *
 * Prosedur (NXP I2C spec §3.1.16):
 * 1. Cek apakah SDA stuck LOW
 * 2. Kirim 9 pulsa clock pada SCL agar slave menyelesaikan byte tertunda
 * 3. Bangkitkan kondisi STOP (SDA LOW→HIGH saat SCL HIGH)
 *
 * Dipanggil SEBELUM Wire.begin() di setiap boot / wake dari deep sleep.
 */
void pulihkanBusI2C() {
  pinMode(PIN_SDA, INPUT_PULLUP);
  pinMode(PIN_SCL, INPUT_PULLUP);
  delay(1);   // beri waktu pull-up stabil

  if (digitalRead(PIN_SDA) == HIGH) return;   // bus bersih, tidak perlu recovery

  Serial.println("I2C SDA stuck LOW — menjalankan bus clear...");
  pinMode(PIN_SCL, OUTPUT);
  for (int i = 0; i < 9; i++) {
    digitalWrite(PIN_SCL, HIGH);
    delayMicroseconds(5);
    digitalWrite(PIN_SCL, LOW);
    delayMicroseconds(5);
  }
  // Kondisi STOP: SDA naik sementara SCL tinggi
  pinMode(PIN_SDA, OUTPUT);
  digitalWrite(PIN_SDA, LOW);
  delayMicroseconds(5);
  digitalWrite(PIN_SCL, HIGH);
  delayMicroseconds(5);
  digitalWrite(PIN_SDA, HIGH);
  delayMicroseconds(5);
  // Kembalikan ke input agar Wire.begin() bisa mengambil alih
  pinMode(PIN_SDA, INPUT_PULLUP);
  pinMode(PIN_SCL, INPUT_PULLUP);
  delay(1);
  Serial.println("Bus clear selesai.");
}

/**
 * Kirim perintah soft reset (0xBA) ke AHT20 di alamat I2C 0x38.
 * Mengembalikan sensor ke keadaan awal tanpa siklus daya (power cycle).
 * Harus dipanggil SETELAH Wire.begin() atau setelah bus clear manual.
 */
void kirimSoftResetAHT20() {
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.beginTransmission(0x38);   // alamat default AHT20
  Wire.write(0xBA);               // perintah soft reset (datasheet Aosong)
  Wire.endTransmission();
  delay(40);                      // datasheet: tunggu ≥20 ms setelah reset
}

void goToSleep() {
  Serial.println("Zzzzz....");
  // Parkir pin I2C sebelum tidur agar tidak floating saat deep sleep.
  // Mencegah slave salah baca sinyal acak dari pin mengambang.
  pinMode(PIN_SDA, INPUT_PULLUP);
  pinMode(PIN_SCL, INPUT_PULLUP);
  Serial.flush();                                       // jangan potong output serial
  ESP.deepSleep((uint64_t)SLEEP_SECONDS * 1000000ULL);  // cast: cegah overflow (m3)
  delay(100);                                           // WAJIB agar SDK benar-benar tidur (M6)
}

void setup() {
  Serial.begin(115200); 
  Serial.println("\n\n--- MULAI ---");

  // Inisialisasi pin jumper konfigurasi WiFi (Pull-up internal)
  pinMode(PIN_CONFIG, INPUT_PULLUP);

  // 1. BACA BATERAI PALING AWAL — radio masih mati, tidak ada sag (M5)
  teganganBaterai = bacaBaterai(rawBaterai);
  Serial.printf("Baterai: %.3f V (raw %d)\n", teganganBaterai, rawBaterai);

  // 2. PROTEKSI DAYA (C2) — sebelum boros energi untuk WiFi
  if (teganganBaterai > 0.5 && teganganBaterai < BATAS_KRITIS) {
    Serial.printf("Baterai kritis (%.2f V) — hibernasi %lu s.\n", teganganBaterai, TIDUR_HEMAT_S);
    Serial.flush();
    ESP.deepSleep((uint64_t)TIDUR_HEMAT_S * 1000000ULL);
    delay(100);          // WAJIB (M6)
    return;
  }

  // 3. SETUP SENSOR I2C (dengan bus recovery — M12)
  //
  // Masalah: selama deep sleep, 3V3 ke sensor tetap menyala tapi GPIO ESP
  // mengambang. Jika transaksi I2C terpotong saat ESP tidur, slave (AHT20
  // atau BMP280) bisa menahan SDA LOW menunggu clock yang tak pernah datang.
  // Wire.begin() bawaan ESP8266 TIDAK melakukan bus clear.
  //
  // Solusi: NXP I2C spec §3.1.16 — 9 pulsa clock pada SCL memaksa slave
  // menyelesaikan byte yang tertunda dan melepas SDA, diikuti kondisi STOP.
  pulihkanBusI2C();
  Wire.begin(PIN_SDA, PIN_SCL);

  // Init sensor dengan satu kali retry jika gagal (bus mungkin baru pulih)
  ahtStatus = aht.begin();
  if (!ahtStatus) {
    Serial.println("AHT20 init gagal, mencoba recovery...");
    pulihkanBusI2C();
    kirimSoftResetAHT20();          // soft reset 0xBA ke alamat 0x38
    Wire.begin(PIN_SDA, PIN_SCL);   // re-init Wire setelah bus clear
    ahtStatus = aht.begin();
    if (!ahtStatus) Serial.println("AHT20 Error (setelah retry)!");
    else Serial.println("AHT20 pulih setelah retry.");
  }

  bmpStatus = bmp.begin(0x77);
  if (!bmpStatus) {
    // Coba alamat alternatif 0x76 (SDO ke GND, umum di modul murah)
    bmpStatus = bmp.begin(0x76);
  }
  if (!bmpStatus) {
    Serial.println("BMP280 init gagal, mencoba recovery...");
    pulihkanBusI2C();
    Wire.begin(PIN_SDA, PIN_SCL);
    bmpStatus = bmp.begin(0x77) || bmp.begin(0x76);
    if (!bmpStatus) Serial.println("BMP280 Error (setelah retry)!");
    else Serial.println("BMP280 pulih setelah retry.");
  }
  if (bmpStatus) {
    // One-shot: benar untuk perangkat deep-sleep (M2).
    // FILTER_OFF karena IIR tak berguna untuk 1 sampel per 5 menit.
    bmp.setSampling(Adafruit_BMP280::MODE_FORCED,
                    Adafruit_BMP280::SAMPLING_X2,     // suhu
                    Adafruit_BMP280::SAMPLING_X16,    // tekanan
                    Adafruit_BMP280::FILTER_OFF,
                    Adafruit_BMP280::STANDBY_MS_1);
  }

  // 4. KONEKSI WIFI (WiFiManager / Deep Sleep Friendly)
  // Masuk portal jika jumper terpasang ke GND (LOW) ATAU belum pernah ada WiFi tersimpan
  bool butuhSetupWiFi = (digitalRead(PIN_CONFIG) == LOW) || (WiFi.SSID().length() == 0);

  if (butuhSetupWiFi) {
    Serial.println("\n>>> MEMASUKI MODE KONFIGURASI WIFI (CAPTIVE PORTAL) <<<");
    Serial.println("1. Hubungkan HP/Laptop ke WiFi: 'MonitoringSuhu-Setup'");
    Serial.println("2. Halaman konfigurasi akan terbuka otomatis (atau buka IP 192.168.4.1)");
    Serial.println("3. Pilih WiFi sekitar, ketik password, lalu klik Simpan.");

    WiFiManager wm;
    wm.setConfigPortalTimeout(180); // Timeout 3 menit untuk melindungi baterai jika ditinggal

    if (!wm.startConfigPortal("MonitoringSuhu-Setup")) {
      Serial.println("Konfigurasi timeout atau gagal terhubung.");
      if (!DEBUG_MODE) { goToSleep(); return; }
    } else {
      Serial.printf("\nWiFi baru tersimpan! Terhubung ke: %s (%s)\n",
                    WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
      Serial.println("PENTING: Segera lepas kabel jumper D5...");
      // Tahan proses sampai jumper benar-benar dicabut oleh pengguna
      while (digitalRead(PIN_CONFIG) == LOW) {
        delay(200);
      }
      Serial.println("Jumper dilepas. Memulai ulang sistem...");
      delay(500);
      ESP.restart();
      return;
    }
  }

  // --- SIKLUS NORMAL (HEMAT DAYA & DEEP SLEEP FRIENDLY) ---
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);          // stop tulis flash tiap wake (cegah flash wear-out)
  WiFi.setOutputPower(17.0);       // default 20.5 dBm berlebihan untuk dalam rumah
  WiFi.begin();                    // panggil kredensial yang tersimpan di flash

  Serial.print("Menghubungkan ke WiFi...");
  unsigned long startKoneksi = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - startKoneksi < 15000)) {
    delay(100);
    Serial.print(".");
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\nGagal Konek (Timeout 15s)!");
    if (!DEBUG_MODE) { goToSleep(); return; }
  } else {
    Serial.printf("\nTerhubung: %s (%d dBm) %s\n",
                  WiFi.SSID().c_str(), WiFi.RSSI(), WiFi.localIP().toString().c_str());
  }

  // 5. CONFIG FIREBASE (Deep Sleep Friendly)
  // Assign URL & Legacy Token
  config.database_url = DATABASE_URL;
  config.signer.tokens.legacy_token = FIREBASE_AUTH;
  
  // OPSI TAMBAHAN PENTING:
  // Reset signin step agar tidak ada cache yang nyangkut
  config.signer.test_mode = false; 
  
  // Inisialisasi
  Firebase.begin(&config, &auth);
  Firebase.reconnectWiFi(true);

  // Tunggu sebentar agar sesi terbentuk (kadang ESP8266 butuh napas)
  delay(100); 

  // Eksekusi khusus Mode Normal (Deep Sleep)
  if (!DEBUG_MODE) {
    processAndSendData();   // fungsi ini tidur sendiri di akhir (M6)
  }
}

void loop() {
  // Eksekusi khusus Mode Debug
  if (DEBUG_MODE) {
    // Pastikan koneksi aktif sebelum memproses data dan menembak server
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("WiFi terputus, mencoba menyambung kembali...");
      WiFi.reconnect();
      unsigned long t0 = millis();
      while (WiFi.status() != WL_CONNECTED && (millis() - t0 < 8000)) {
        delay(100);
      }
    }
    if (WiFi.status() == WL_CONNECTED) {
      processAndSendData();
    }
    delay(10000); // Tunggu 10 detik sebelum push data lagi
  }
}

void processAndSendData() {
  // 3. BACA DATA
  float t = 0, h = 0, p = 0;
  
  if (ahtStatus) {
    sensors_event_t humidity, temp;
    aht.getEvent(&humidity, &temp);      // bacaan pertama dibuang: AHT20 belum siap (M1)
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

  if (bmpStatus) {
    if (bmp.takeForcedMeasurement()) {   // M2: pastikan konversi selesai
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
  
  Serial.printf("Suhu: %.2f, Hum: %.2f, Tekanan: %.2f, VCC: %.2fV\n", t, h, p, teganganBaterai);

  // M9: jangan return dini — tetap kirim daya saat kedua sensor gagal
  bool semuaSensorGagal = (!ahtStatus && !bmpStatus);
  if (semuaSensorGagal) {
    Serial.println("Semua sensor error — tetap kirim data daya untuk diagnosis.");
  }

  // 6. PUSH KE FIREBASE
  FirebaseJson json;
  
  if (ahtStatus) {
    json.set("suhu", t);
    json.set("kelembapan", h);
  }
  if (bmpStatus) {
    json.set("tekanan", p);
  }
  
  json.set("tegangan_vcc", teganganBaterai);
  json.set("koneksi", WiFi.SSID());
  json.set("timestamp/.sv", "timestamp");

  // C2: status daya untuk dashboard (field baru, opsional — absen = tidak diketahui)
  const char* statusDaya = (teganganBaterai < BATAS_KRITIS)     ? "kritis"
                         : (teganganBaterai < BATAS_PERINGATAN) ? "rendah"
                                                                : "normal";
  json.set("status_daya", statusDaya);

  // M11: field diagnostik (baru, opsional)
  if (semuaSensorGagal) json.set("status_sensor", "semua_gagal");
  json.set("rssi", WiFi.RSSI());                    // kekuatan sinyal, dBm
  json.set("durasi_bangun_ms", (int)millis());      // set TEPAT sebelum push
  json.set("adc_raw", rawBaterai);                  // pisahkan mentah dari terkalibrasi

  // M8: retry push dengan backoff linier
  const int MAKS_PERCOBAAN_PUSH = 3;
  for (int percobaan = 1; percobaan <= MAKS_PERCOBAAN_PUSH; percobaan++) {
    Serial.printf("Push ke Firebase (percobaan %d/%d)... ", percobaan, MAKS_PERCOBAAN_PUSH);
    if (Firebase.RTDB.pushJSON(&fbdo, "/RIWAYAT_LAB", &json)) {
      Serial.println("Berhasil!");
      break;
    }
    Serial.printf("Gagal: %s\n", fbdo.errorReason().c_str());
    if (percobaan < MAKS_PERCOBAAN_PUSH) delay(1000 * percobaan);   // backoff linier
  }

  if (!DEBUG_MODE) goToSleep();   // M6: return/tidur eksplisit setelah push
}