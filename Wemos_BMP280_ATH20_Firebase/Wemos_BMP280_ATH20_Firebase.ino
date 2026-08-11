#include <ESP8266WiFi.h>
#include <ESP8266WiFiMulti.h>
#include <Wire.h>
#include <Adafruit_AHTX0.h>
#include <Adafruit_BMP280.h>
#include <Firebase_ESP_Client.h>

// ADC_MODE(ADC_VCC); // Dihapus karena kita akan menggunakan Pin A0 untuk baca baterai fisik

// --- PERBAIKAN UTAMA: WAJIB ADA ADDONS INI ---
// Tanpa ini, Legacy Token tidak bisa diproses oleh library
#include <addons/TokenHelper.h> 
#include <addons/RTDBHelper.h> 

// --- KREDENSIAL RAHASIA (DIAMBIL DARI secrets.h - TER-GITIGNORE) ---
#include "secrets.h"

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
ESP8266WiFiMulti wifiMulti;
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

void goToSleep() {
  Serial.println("Zzzzz....");
  Serial.flush();                                       // jangan potong output serial
  ESP.deepSleep((uint64_t)SLEEP_SECONDS * 1000000ULL);  // cast: cegah overflow (m3)
  delay(100);                                           // WAJIB agar SDK benar-benar tidur (M6)
}

void setup() {
  Serial.begin(115200); 
  Serial.println("\n\n--- MULAI ---");

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

  // 3. SETUP SENSOR I2C
  Wire.begin(); 

  if (!aht.begin()) {
    Serial.println("AHT20 Error!");
    ahtStatus = false;
  }
  if (!bmp.begin(0x77)) { 
    Serial.println("BMP280 Error!");
    bmpStatus = false;
  } else {
    // One-shot: benar untuk perangkat deep-sleep (M2).
    // FILTER_OFF karena IIR tak berguna untuk 1 sampel per 5 menit.
    bmp.setSampling(Adafruit_BMP280::MODE_FORCED,
                    Adafruit_BMP280::SAMPLING_X2,     // suhu
                    Adafruit_BMP280::SAMPLING_X16,    // tekanan
                    Adafruit_BMP280::FILTER_OFF,
                    Adafruit_BMP280::STANDBY_MS_1);
  }

  // 4. KONEKSI WIFI — SATU panggilan run(timeout), bukan loop (M7)
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);          // stop tulis flash tiap wake
  WiFi.setOutputPower(17.0);       // default 20,5 dBm berlebihan untuk dalam rumah
  
  wifiMulti.addAP(WIFI_SSID_1, WIFI_PASS_1);          
  wifiMulti.addAP(WIFI_SSID_2, WIFI_PASS_2); 
  wifiMulti.addAP(WIFI_SSID_3, WIFI_PASS_3); 
  wifiMulti.addAP(WIFI_SSID_4, WIFI_PASS_4); 
  wifiMulti.addAP(WIFI_SSID_5, WIFI_PASS_5); 

  Serial.print("Mencari WiFi...");
  if (wifiMulti.run(20000) != WL_CONNECTED) {
    Serial.println("\nGagal Konek!");
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
      wifiMulti.run();
    } else {
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