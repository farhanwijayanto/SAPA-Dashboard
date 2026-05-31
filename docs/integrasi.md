# Panduan Integrasi SAPA — Dashboard VPS ↔ Edge Server (AI Face Recognition) ↔ ESP32

Dokumen ini menjelaskan **langkah demi langkah** cara menghubungkan tiga komponen SAPA:

1. **Dashboard di VPS** (`https://sapa.farhn.dev`) — backend FastAPI + frontend + database + MQTT broker
2. **Edge Server** (laptop dekat gate) — kamera + AI face recognition
3. **ESP32** (hardware gate) — servo + buzzer + sensor

> **Penanda lokasi command:**
> - 🖥️ **[VPS]** = jalankan di terminal VPS (SSH ke Google Cloud)
> - 💻 **[LAPTOP EDGE]** = jalankan di PowerShell laptop dekat gate
> - 🔧 **[ARDUINO IDE]** = edit + flash di Arduino IDE
> - 🌐 **[BROWSER]** = buka di browser

---

## Daftar Isi

1. [Gambaran Arsitektur](#1-gambaran-arsitektur)
2. [Alur Kerja End-to-End](#2-alur-kerja-end-to-end)
3. [Prasyarat](#3-prasyarat)
4. [Langkah 1 — Siapkan Credential di VPS](#4-langkah-1--siapkan-credential-di-vps)
5. [Langkah 2 — Setup Edge Server di Laptop](#5-langkah-2--setup-edge-server-di-laptop)
6. [Langkah 3 — Jalankan Edge Server](#6-langkah-3--jalankan-edge-server)
7. [Langkah 4 — Tambah Karyawan + Foto Wajah dari Dashboard](#7-langkah-4--tambah-karyawan--foto-wajah-dari-dashboard)
8. [Langkah 5 — Setup ESP32 Gate](#8-langkah-5--setup-esp32-gate)
9. [Langkah 6 — Verifikasi Integrasi Penuh](#9-langkah-6--verifikasi-integrasi-penuh)
10. [Cara Update Kode](#10-cara-update-kode)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Gambaran Arsitektur

```
┌──────────────────────────────────────────────────────────────────────┐
│  VPS GOOGLE CLOUD (Ubuntu + Kubernetes)                               │
│  Domain: https://sapa.farhn.dev                                       │
│                                                                       │
│   ┌─────────────┐   ┌──────────────┐   ┌──────────┐  ┌─────────────┐ │
│   │  Frontend   │   │   Backend    │   │ Postgres │  │  MongoDB    │ │
│   │  (React)    │   │  (FastAPI)   │   │ (data    │  │ (attendance │ │
│   │  Dashboard  │   │  + AI Gate   │   │ karyawan)│  │  logs)      │ │
│   └─────────────┘   └──────┬───────┘   └──────────┘  └─────────────┘ │
│                            │                                          │
│                     ┌──────▼───────┐                                  │
│                     │  Mosquitto   │  MQTT broker (port 31883)        │
│                     │  MQTT broker │                                  │
│                     └──────┬───────┘                                  │
└────────────────────────────┼──────────────────────────────────────────┘
            ▲ HTTPS 443       │ MQTT 31883          ▲ MQTT 31883
            │                 │                     │
            │ (1) face-match  │ (3) gate command    │
            │ (2) push frame  ▼                     │
┌───────────┴──────────┐                  ┌─────────┴──────────┐
│  LAPTOP EDGE          │                  │  ESP32 GATE         │
│  - Webcam             │                  │  - Servo (buka gate)│
│  - AI face_recognition│                  │  - Buzzer           │
│  - Python service     │                  │  - Ultrasonic/PIR   │
│  - Kotak hijau/merah  │                  │                     │
└───────────────────────┘                  └─────────────────────┘
```

**Tiga jalur komunikasi:**

| Jalur | Dari → Ke | Protokol | Fungsi |
|-------|-----------|----------|--------|
| 1 | Laptop Edge → VPS Backend | HTTPS 443 | Sync foto wajah, lapor hasil match, kirim frame kamera |
| 2 | VPS Backend → Mosquitto | internal | Publish perintah buka gate |
| 3 | Mosquitto → ESP32 | MQTT 31883 | ESP32 terima perintah buka servo |

---

## 2. Alur Kerja End-to-End

Berikut yang terjadi dari awal sampai gate terbuka:

```
LANGKAH A: Manager daftar karyawan baru
  🌐 Dashboard → Add Employee
     → input nama, tanggal lahir, posisi, + upload foto wajah
     → foto tersimpan di VPS (backend/uploads/faces/{id}.jpg)

LANGKAH B: Edge laptop sync otomatis (tiap 60 detik)
  💻 edge_server otomatis:
     → GET daftar foto dari VPS
     → download foto karyawan baru
     → encode wajah jadi 128 angka (embedding) pakai AI
     → simpan ke cache embeddings.npz
     → wajah baru SIAP dikenali (tanpa restart, tanpa training manual)

LANGKAH C: Karyawan datang ke gate
  💻 Kamera laptop menangkap wajah:
     → AI deteksi + cocokkan dengan cache
     → COCOK   → kotak HIJAU + nama karyawan
     → TIDAK   → kotak MERAH + "Unknown"

LANGKAH D: Kalau wajah valid
  💻 edge → POST /api/edge/face-match ke VPS
  🖥️ VPS → simpan log absensi ke MongoDB
  🖥️ VPS → publish perintah "open" ke MQTT topic sapa/gate
  🔧 ESP32 → terima perintah → servo buka + buzzer bunyi 1×
  🌐 Dashboard → log absensi muncul realtime

LANGKAH E: Kalau wajah tidak dikenali
  💻 edge → POST /api/edge/face-match (is_valid=false)
  🖥️ VPS → publish "invalid" ke sapa/gate
  🔧 ESP32 → buzzer bunyi 2× (gate tetap tutup)
```

---

## 3. Prasyarat

### Di VPS (sudah jalan)
- Dashboard SAPA sudah ter-deploy di `https://sapa.farhn.dev`
- Kubernetes (kubeadm) dengan namespace `sapa` aktif
- Mosquitto MQTT broker aktif di NodePort 31883

### Di Laptop Edge
- Windows 10 (21H2+) atau Windows 11
- Webcam (internal atau USB)
- RAM minimal 4 GB (disarankan 8 GB)
- Koneksi internet ke `https://sapa.farhn.dev` dan MQTT port 31883
- Python 3.11, Git, Visual C++ Build Tools, CMake
  (cara install lihat `docs/SAPA-Kubeadm-Deployment-ID.md` §8.A.2)

### Di ESP32
- Board ESP32 + servo + buzzer + sensor ultrasonic/PIR
- Arduino IDE dengan library: `PubSubClient`, `ArduinoJson`, `ESP32Servo`

---

## 4. Langkah 1 — Siapkan Credential di VPS

Edge server butuh **2 credential** dari VPS: `EDGE_INGEST_KEY` (untuk HTTPS) dan password MQTT user `edge` (untuk heartbeat). Ambil keduanya di VPS.

### 4.1 SSH ke VPS Google Cloud

Pilih salah satu cara:

**Cara A — Lewat browser (paling mudah):**
1. 🌐 Buka https://console.cloud.google.com/compute/instances
2. Cari VM SAPA Anda → klik tombol **SSH** di kolom Connect
3. Terminal browser terbuka otomatis

**Cara B — Lewat gcloud CLI:**
```bash
# 💻 [LAPTOP] kalau sudah install gcloud
gcloud compute ssh NAMA_VM --zone=ZONA_VM
```

### 4.2 Ambil EDGE_INGEST_KEY

```bash
# 🖥️ [VPS]
kubectl -n sapa get secret sapa-backend-secret \
  -o jsonpath='{.data.EDGE_INGEST_KEY}' | base64 -d; echo
```

Output (contoh): `a1b2c3d4e5f6789abcdef...` (48 karakter hex)

**Kalau output KOSONG**, berarti belum dibuat. Generate baru:

```bash
# 🖥️ [VPS]
EDGE_KEY=$(openssl rand -hex 24)
kubectl -n sapa patch secret sapa-backend-secret \
  --type=merge -p "{\"stringData\":{\"EDGE_INGEST_KEY\":\"$EDGE_KEY\"}}"
kubectl -n sapa rollout restart deploy/sapa-backend
echo "EDGE_INGEST_KEY=$EDGE_KEY"
```

**SALIN nilai ini ke catatan/password manager.** Akan dipakai di Langkah 2.

### 4.3 Siapkan password MQTT user `edge`

Password MQTT disimpan sebagai **hash** (tidak bisa dibaca balik). Kalau Anda **belum pernah menyimpan** password-nya, reset ulang:

```bash
# 🖥️ [VPS]
cd /opt/sapa

# Generate 3 password baru
BACKEND_PW=$(openssl rand -hex 12)
EDGE_PW=$(openssl rand -hex 12)
ESP32_PW=$(openssl rand -hex 12)

# Buat file hash pakai docker mosquitto
docker run --rm -v "$PWD:/work" eclipse-mosquitto:2 sh -c "
  mosquitto_passwd -c -b /work/passwords backend '$BACKEND_PW' &&
  mosquitto_passwd -b   /work/passwords edge    '$EDGE_PW' &&
  mosquitto_passwd -b   /work/passwords esp32   '$ESP32_PW'
"

# Update secret Mosquitto
kubectl -n sapa delete secret sapa-mosquitto-auth --ignore-not-found
kubectl -n sapa create secret generic sapa-mosquitto-auth \
  --from-file=passwords=./passwords
rm -f passwords

# Update password backend di secret backend (otomatis dipakai backend publish gate)
kubectl -n sapa patch secret sapa-backend-secret --type=merge \
  -p "{\"stringData\":{\"MQTT_USERNAME\":\"backend\",\"MQTT_PASSWORD\":\"$BACKEND_PW\"}}"

# Restart Mosquitto + backend
kubectl -n sapa rollout restart deploy/sapa-mosquitto
kubectl -n sapa rollout restart deploy/sapa-backend
kubectl -n sapa rollout status deploy/sapa-mosquitto --timeout=2m

# PRINT — SIMPAN KETIGANYA KE PASSWORD MANAGER
echo "=============================================="
echo "MQTT_BACKEND_PASSWORD=$BACKEND_PW  (otomatis di secret backend)"
echo "MQTT_EDGE_PASSWORD=$EDGE_PW        (untuk laptop edge - Langkah 2)"
echo "MQTT_ESP32_PASSWORD=$ESP32_PW      (untuk ESP32 - Langkah 5)"
echo "=============================================="
```

> **Catatan:** kalau Anda sudah punya password lama tersimpan, lewati 4.3 dan pakai password yang ada.

### 4.4 Catatan credential yang Anda butuhkan

Setelah Langkah 1, pastikan Anda punya:

| Credential | Dipakai di | Dari |
|------------|-----------|------|
| `EDGE_INGEST_KEY` | Laptop edge + ESP32 (HTTPS) | step 4.2 |
| `MQTT_EDGE_PASSWORD` | Laptop edge (MQTT heartbeat) | step 4.3 |
| `MQTT_ESP32_PASSWORD` | ESP32 (MQTT gate command) | step 4.3 |
| Password dashboard `manager` | Laptop edge (auto-login sync foto) | password login Anda |

---

## 5. Langkah 2 — Setup Edge Server di Laptop

Semua langkah ini di **💻 [LAPTOP EDGE]** (PowerShell sebagai user biasa).

### 5.1 Install tools (sekali saja)

```powershell
# 💻 [LAPTOP EDGE] PowerShell Administrator
winget install --id Python.Python.3.11 -e --accept-source-agreements --accept-package-agreements
winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
winget install --id Microsoft.VisualStudio.2022.BuildTools -e --accept-package-agreements `
  --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --quiet --wait --norestart"
winget install --id Kitware.CMake -e --accept-package-agreements
```

Tutup & buka ulang PowerShell, lalu verifikasi:

```powershell
# 💻 [LAPTOP EDGE]
py -3.11 --version
git --version
cmake --version
```

### 5.2 Clone repo + buat virtualenv

```powershell
# 💻 [LAPTOP EDGE]
cd C:\
git clone https://github.com/farhanwijayanto/SAPA-Dashboard.git sapa
cd C:\sapa

py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1

python -m pip install --upgrade pip wheel setuptools
pip install -r edge_server\requirements.txt
```

> Build pertama kali bisa 5–15 menit karena `dlib` di-compile dari source.
> Kalau gagal, lihat alternatif (wheel pra-kompilasi / mediapipe) di
> `docs/SAPA-Kubeadm-Deployment-ID.md` §8.A.3.

### 5.3 Konfigurasi file `.env`

```powershell
# 💻 [LAPTOP EDGE]
Copy-Item edge_server\.env.example edge_server\.env
notepad edge_server\.env
```

Isi nilai-nilai berikut (pakai credential dari Langkah 1):

```env
SAPA_API_BASE=https://sapa.farhn.dev/api
SAPA_USERNAME=manager
SAPA_PASSWORD=<password login manager Anda>
EDGE_INGEST_KEY=<EDGE_INGEST_KEY dari step 4.2>
MQTT_BROKER=sapa.farhn.dev
MQTT_PORT=31883
MQTT_USERNAME=edge
MQTT_PASSWORD=<MQTT_EDGE_PASSWORD dari step 4.3>
CAMERA_INDEX=0
RECOGNITION_THRESHOLD=0.55
COOLDOWN_SECONDS=5
SYNC_EVERY_SECONDS=60
FRAME_PUSH_FPS=2
```

> **PENTING:** `SAPA_API_BASE` HARUS pakai `/api` di belakang. Tanpa itu,
> semua request akan 404.

### 5.4 (Opsional) Izinkan firewall egress

```powershell
# 💻 [LAPTOP EDGE] PowerShell Administrator (hanya kalau firewall ketat)
New-NetFirewallRule -DisplayName "SAPA MQTT egress" `
  -Direction Outbound -Protocol TCP -RemotePort 31883 -Action Allow
New-NetFirewallRule -DisplayName "SAPA HTTPS egress" `
  -Direction Outbound -Protocol TCP -RemotePort 443 -Action Allow
```

---

## 6. Langkah 3 — Jalankan Edge Server

```powershell
# 💻 [LAPTOP EDGE]
cd C:\sapa
.\venv\Scripts\Activate.ps1
python -m edge_server.main
```

### 6.1 Log yang menandakan SUKSES

```
SAPA_API_BASE = https://sapa.farhn.dev/api
MQTT connected rc=0
Synced 3 faces -> 3 embeddings
Frame push OK -> dashboard Live Camera aktif
```

- `MQTT connected rc=0` → MQTT nyambung (heartbeat jalan)
- `Synced N faces` → foto karyawan berhasil di-download + di-encode AI
- `Frame push OK` → **kamera berhasil terhubung ke dashboard**

### 6.2 Window kamera lokal

Akan muncul window **"SAPA Edge"** di laptop dengan:
- **Kotak HIJAU** mengelilingi wajah yang dikenali + label `513061 - John Doe (91%)`
- **Kotak MERAH** mengelilingi wajah tidak dikenali + label `Unknown`
- Banner atas: `SAPA Edge | faces=3 | MQTT=OK | 14.5 fps`
- Tombol `[Q]` keluar, `[R]` paksa sync ulang

### 6.3 Lihat kamera di Dashboard

> **PENTING — bedakan dua "kamera" di dashboard:**
>
> | Lokasi | Sumber kamera |
> |--------|---------------|
> | **Dashboard → System → Live Camera** | Frame dari edge server Python (yang baru Anda jalankan) |
> | Halaman `https://sapa.farhn.dev/edge` | Webcam BROWSER yang membuka halaman itu (bukan edge server) |
>
> Jadi kamera dari `python -m edge_server.main` muncul di
> **Dashboard → System tab → Live Camera**, BUKAN di halaman `/edge`.

🌐 Buka `https://sapa.farhn.dev/` → login manager → tab **System** → **Live Camera**.

---

## 7. Langkah 4 — Tambah Karyawan + Foto Wajah dari Dashboard

Ini bagian yang membuat AI mengenali karyawan baru otomatis.

### 7.1 Tambah karyawan baru

1. 🌐 Buka `https://sapa.farhn.dev/` → login sebagai **manager**
2. Klik menu **Add Employee** (sidebar kiri)
3. Isi form:
   - **Nama** (mis. "Budi Santoso")
   - **Tanggal Lahir**
   - **Posisi/Divisi**
   - **Foto wajah** — pilih file foto wajah jelas (frontal, terang) atau capture dari webcam
4. Klik **Add Employee**

Yang terjadi otomatis di belakang layar:
- Frontend simpan data karyawan ke Postgres (via `POST /employees/`)
- Frontend upload foto ke VPS (via `POST /employees/{id}/face`)
- Foto tersimpan di `backend/uploads/faces/{id}.jpg`

### 7.2 Tunggu sync edge (otomatis)

Dalam **maksimal 60 detik**, edge laptop akan otomatis:
- Detect ada foto baru
- Download + encode wajah
- Update cache

Di log laptop akan muncul:
```
Synced 4 faces -> 4 embeddings    (sebelumnya 3, sekarang 4)
```

**Mau langsung tanpa nunggu 60 detik?** Tekan tombol **`R`** di window "SAPA Edge" untuk paksa sync.

### 7.3 Test recognition

Arahkan wajah karyawan yang baru didaftar ke kamera laptop:
- Kotak **HIJAU** + nama muncul → berhasil dikenali
- Gate terbuka (kalau ESP32 sudah terhubung — lihat Langkah 5)

---

## 8. Langkah 5 — Setup ESP32 Gate

### 8.1 Buka firmware di Arduino IDE

🔧 Buka file `esp32/sapa_gate.ino` (versi PIR) atau
`esp32/sapa_gate_ultrasonic.ino` (versi HC-SR04) di Arduino IDE.

### 8.2 Edit credential

🔧 Ubah baris konfigurasi di atas:

```cpp
const char* WIFI_SSID     = "nama-wifi-anda";
const char* WIFI_PASS     = "password-wifi";
const char* MQTT_HOST     = "sapa.farhn.dev";
const uint16_t MQTT_PORT  = 31883;
const char* MQTT_USER     = "esp32";
const char* MQTT_PASS     = "<MQTT_ESP32_PASSWORD dari step 4.3>";
```

### 8.3 Install library (sekali saja)

🔧 Arduino IDE → Tools → Manage Libraries → install:
- `PubSubClient` (by Nick O'Leary)
- `ArduinoJson` (by Benoit Blanchon)
- `ESP32Servo`

### 8.4 Wiring hardware

**Versi PIR (`sapa_gate.ino`):**
| Komponen | Pin ESP32 |
|----------|-----------|
| Servo | GPIO 13 |
| Buzzer | GPIO 14 |
| PIR sensor | GPIO 27 |
| LED indikator | GPIO 2 |

**Versi Ultrasonic (`sapa_gate_ultrasonic.ino`):**
| Komponen | Pin ESP32 | Catatan |
|----------|-----------|---------|
| Servo | GPIO 13 | |
| Buzzer | GPIO 14 | |
| HC-SR04 TRIG | GPIO 26 | |
| HC-SR04 ECHO | GPIO 25 | **WAJIB voltage divider** (1kΩ+2kΩ) karena ECHO 5V |
| LED | GPIO 2 | |

> ⚠️ Pin ECHO HC-SR04 keluarkan 5V, sedangkan ESP32 maksimal 3.3V.
> Pasang voltage divider: ECHO → 1kΩ → GPIO25, lalu GPIO25 → 2kΩ → GND.

### 8.5 Flash + cek Serial Monitor

🔧 Upload ke ESP32, lalu buka Serial Monitor (115200 baud):

```
WiFi OK 192.168.x.x
MQTT...OK
```

Kalau `MQTT...rc=5` → password user `esp32` salah. Cek lagi step 4.3 + 8.2.

---

## 9. Langkah 6 — Verifikasi Integrasi Penuh

Lakukan urutan ini untuk memastikan ketiga jalur tersambung.

### 9.1 Cek backend hidup

```bash
# 🖥️ [VPS] atau dari mana saja
curl -fsS https://sapa.farhn.dev/api/health
# Output: {"ok":true}
```

### 9.2 Cek endpoint AI Gate

```bash
# 🖥️ [VPS]
EDGE_KEY=$(kubectl -n sapa get secret sapa-backend-secret \
  -o jsonpath='{.data.EDGE_INGEST_KEY}' | base64 -d)

# Daftar wajah
curl -fsS -H "X-EDGE-KEY: $EDGE_KEY" https://sapa.farhn.dev/api/edge/faces
# Output: {"faces":[{"employee_id":"513061","url":"..."}]}
```

### 9.3 Cek frame kamera dari edge masuk ke dashboard

1. 💻 Pastikan `python -m edge_server.main` masih jalan di laptop
2. 🌐 Buka Dashboard → System → Live Camera → harus tampil feed kamera laptop realtime

### 9.4 Cek face-match → MQTT → ESP32

1. 💻 Arahkan wajah karyawan terdaftar ke kamera laptop
2. Pantau MQTT dari VPS:
   ```bash
   # 🖥️ [VPS] terminal terpisah
   MQTT_BACKEND_PW=$(kubectl -n sapa get secret sapa-backend-secret \
     -o jsonpath='{.data.MQTT_PASSWORD}' | base64 -d)
   kubectl -n sapa exec deploy/sapa-mosquitto -- mosquitto_sub \
     -h 127.0.0.1 -u backend -P "$MQTT_BACKEND_PW" -t sapa/gate -v -C 3
   ```
3. Saat wajah cocok, harus muncul:
   ```
   sapa/gate {"action": "open", "employee_id": "513061", "ts": "..."}
   ```
4. 🔧 ESP32 servo terbuka + buzzer bunyi 1×
5. 🌐 Dashboard → tab Attendance → log absensi baru muncul

### 9.5 Checklist sukses penuh

- [ ] `curl /api/health` → `{"ok":true}`
- [ ] Log laptop: `Frame push OK -> dashboard Live Camera aktif`
- [ ] Log laptop: `Synced N faces -> N embeddings`
- [ ] Dashboard System → Live Camera tampil feed kamera laptop
- [ ] Wajah terdaftar → kotak HIJAU di window edge
- [ ] `mosquitto_sub sapa/gate` capture `{"action":"open"}`
- [ ] ESP32 servo buka + buzzer bunyi
- [ ] Dashboard Attendance → log absensi muncul

Kalau semua tercentang, integrasi **berhasil penuh**.

---

## 10. Cara Update Kode

Kalau ada perubahan kode di GitHub, update kedua sisi.

### 10.1 Update VPS (backend + frontend)

```bash
# 🖥️ [VPS]
cd /opt/sapa
git pull origin main

# Rebuild image dengan tag baru (naikkan versi)
docker build --network=host -t sapa-backend:1.3 ./backend
docker build --network=host -t sapa-frontend:1.3 ./frontend

# Import ke containerd
docker save sapa-backend:1.3  | sudo ctr -n k8s.io images import -
docker save sapa-frontend:1.3 | sudo ctr -n k8s.io images import -

# Rolling update (zero downtime)
kubectl -n sapa set image deploy/sapa-backend  backend=sapa-backend:1.3
kubectl -n sapa set image deploy/sapa-frontend frontend=sapa-frontend:1.3
kubectl -n sapa rollout status deploy/sapa-backend --timeout=5m
```

**Kalau ada masalah, rollback cepat:**
```bash
# 🖥️ [VPS]
kubectl -n sapa rollout undo deploy/sapa-backend
kubectl -n sapa rollout undo deploy/sapa-frontend
```

### 10.2 Update Edge Server (laptop)

```powershell
# 💻 [LAPTOP EDGE]
cd C:\sapa
git pull origin main
.\venv\Scripts\Activate.ps1
pip install -r edge_server\requirements.txt   # kalau ada dependency baru

# Restart service:
# - kalau jalan manual: Ctrl+C lalu python -m edge_server.main
# - kalau pakai NSSM:    nssm restart sapa-edge
```

### 10.3 Update ESP32

🔧 Pull kode terbaru, buka `.ino` di Arduino IDE, flash ulang.

---

## 11. Troubleshooting

### Edge server tidak konek ke dashboard

| Gejala (log laptop) | Penyebab | Solusi |
|---------------------|----------|--------|
| `Frame push gagal (HTTP 401)` | `EDGE_INGEST_KEY` salah/kosong | Cek ulang step 4.2, samakan di `.env` |
| `Frame push gagal (HTTP 404)` | `SAPA_API_BASE` tanpa `/api` | Set `SAPA_API_BASE=https://sapa.farhn.dev/api` |
| `Frame push gagal (HTTP 413)` | Frame terlalu besar | Sudah di-handle (640x480 quality 70) |
| `Synced 0 faces` | Auto-login gagal / belum ada foto | Isi `SAPA_PASSWORD`. Upload foto via dashboard |

### Kamera tidak muncul di dashboard

| Gejala | Solusi |
|--------|--------|
| Live Camera kosong di System tab | Pastikan log laptop `Frame push OK`. Frame stale >5 detik akan hilang |
| Salah lihat di halaman `/edge` | Kamera edge muncul di **System → Live Camera**, bukan halaman `/edge` |

### Webcam tidak terbuka

| Gejala | Solusi |
|--------|--------|
| `Gagal buka camera index 0` | Webcam dipakai app lain, atau index salah → coba `CAMERA_INDEX=1` |
| Window hitam | Izin kamera Windows mati → Settings → Privacy → Camera → "Let desktop apps access your camera = On" |

### MQTT tidak konek

| Gejala | Solusi |
|--------|--------|
| `MQTT connect failed` | Cek port 31883 tidak diblokir firewall |
| `Connection Refused: not authorised` | Password user `edge` salah → reset di step 4.3 |

> **Catatan:** kalau MQTT bermasalah, recognition + buka gate **tetap jalan**
> lewat HTTPS (`/api/edge/face-match`). MQTT hanya untuk heartbeat "gate online".
> Yang WAJIB punya MQTT adalah ESP32 (untuk terima perintah gate).

### ESP32 tidak buka gate

| Gejala | Solusi |
|--------|--------|
| `MQTT rc=5` di Serial Monitor | Password user `esp32` salah |
| Servo tidak gerak walau MQTT OK | Cek wiring servo GPIO 13, cek `mosquitto_sub sapa/gate` ada pesan masuk |
| Wajah tidak dikenali padahal sudah daftar | Tunggu sync (`Synced N`), atau tekan `R`. Turunkan `RECOGNITION_THRESHOLD` ke 0.6 |

### Wajah tidak dikenali (selalu kotak merah)

| Penyebab | Solusi |
|----------|--------|
| Belum sync | Tunggu 60 detik atau tekan `R` di window |
| Foto referensi tidak ada wajah jelas | Re-upload foto frontal yang terang |
| Threshold terlalu ketat | Naikkan `RECOGNITION_THRESHOLD` dari 0.55 ke 0.60-0.65 |
| `faces=0` di banner | Foto belum ke-sync — cek `EDGE_INGEST_KEY` + `SAPA_PASSWORD` |

---

## Ringkasan Urutan Singkat

```
1. 🖥️  [VPS]    Ambil EDGE_INGEST_KEY + reset password MQTT (Langkah 1)
2. 💻 [LAPTOP]  git clone + pip install + isi .env (Langkah 2)
3. 💻 [LAPTOP]  python -m edge_server.main (Langkah 3)
4. 🌐 [BROWSER] Add Employee + upload foto wajah (Langkah 4)
5. 🔧 [ARDUINO] Flash ESP32 dengan credential (Langkah 5)
6. ✅ [SEMUA]   Verifikasi end-to-end (Langkah 6)
```

Selesai. Dengan urutan ini, wajah karyawan yang didaftar lewat dashboard akan
otomatis tersinkron ke edge server, dikenali kamera (kotak hijau/merah), dan
saat valid akan membuka gate ESP32 sambil mencatat absensi ke dashboard.
