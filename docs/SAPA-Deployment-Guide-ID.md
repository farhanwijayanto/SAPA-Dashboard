# SAPA Deployment & Integrasi (VPS Ubuntu + Nginx + Kubernetes + MQTT + Edge Server + ESP32)

Dokumen ini menjelaskan instalasi end-to-end SAPA setelah penambahan fitur:
- Live camera dashboard mengambil frame dari `/edge` (kamera browser di-Dashboard sudah dinonaktifkan).
- Tombol **Open / Close Gate** manual untuk **manager + admin** di tab System, lengkap dengan badge status MQTT (online / offline) dari perangkat IoT.
- Edge Server (Python) menjalankan model **AI Face Recognition** secara lokal, menarik gambar referensi dari VPS.
- ESP32 (servo + buzzer + PIR/Ultrasonik) terhubung ke MQTT (`sapa/gate`, `sapa/pir`, `sapa/device/heartbeat`).
- Login admin/manajer (sukses + gagal) tersimpan di MongoDB VPS dengan format **(Username, Status, Timestamp)**, dan ditampilkan real-time di dashboard.
- Tombol **View Faces** di tabel Employees untuk melihat foto wajah yang tersimpan.

## 1) Arsitektur

```
┌────────┐  HTTPS  ┌───────────────┐
│ Browser│────────▶│ Nginx (VPS)   │
└────────┘         │  ├ /          │ static frontend (React build)
                   │  └ /api/*     │ reverse proxy ke FastAPI :8080
                   └───────┬───────┘
                           │
            ┌──────────────┼─────────────────┐
            ▼              ▼                 ▼
   ┌────────────────┐  ┌─────────────┐ ┌──────────────┐
   │ FastAPI        │  │ MongoDB (k8s)│ │ Mosquitto    │
   │ - /edge/frame  │  │ attendance + │ │ (k8s atau    │
   │ - /edge/face-  │  │ audit logs   │ │  host)       │
   │   match        │  └──────────────┘ └─────┬────────┘
   │ - /gate/*      │                         │MQTT pub/sub
   │ - /iot/status  │                         │
   │ - /audit/logins│                         │
   └────────┬───────┘                         │
            │ HTTPS                           │
            ▼                                 ▼
   ┌──────────────────┐  MQTT (LAN)   ┌──────────────────┐
   │ Edge Server      │──────────────▶│ ESP32 (gate)     │
   │ - camera capture │               │ servo + buzzer + │
   │ - face_recognition (AI)          │ PIR/Ultrasonik   │
   │ - sync embeddings dari VPS       │                  │
   └──────────────────┘               └──────────────────┘
```

Topik MQTT yang dipakai:

| topic | arah | sumber pemicu | contoh payload |
|-------|------|---------------|----------------|
| `sapa/gate` | publish | POST /api/edge/face-match di AI_Gate_Module | `{"action": "open", "employee_id": "513061"}` |
| `sapa/attendance` | subscribe | ESP32/Edge melaporkan ke Backend_App | `{ "employee_id": "123456", "is_valid": true, "direction": "in" }` |
| `sapa/device/heartbeat` | subscribe | ESP32/Edge mengirim heartbeat ke Backend_App | `{ "online": true, "source": "esp32" }` (≤15s = ONLINE) |
| `sapa/pir` | subscribe | ESP32 mendeteksi pergerakan PIR | `{ "motion": true }` lalu Backend_App kirim `close` ke gate |

## 2) Prasyarat VPS

- Ubuntu 22.04 LTS, public IP, domain `sapa.example.com` ke A record.
- Akses SSH (key-based), user `deploy`.
- Software: Nginx + Certbot, Python 3.11, Node 18+, k3s (Kubernetes lite).

```bash
sudo apt update && sudo apt -y upgrade
sudo apt install -y nginx ufw fail2ban git build-essential cmake \
  python3.11 python3.11-venv python3-pip ca-certificates curl
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs
sudo apt install -y certbot python3-certbot-nginx
```

## 3) Install k3s + namespace SAPA

```bash
curl -sfL https://get.k3s.io | sh -s - --disable traefik
sudo kubectl get nodes
kubectl apply -f k8s/namespace.yaml
```

## 4) Deploy MongoDB + Mosquitto di k3s

```bash
kubectl apply -f k8s/mongo-deployment.yaml
kubectl apply -f k8s/mqtt-deployment.yaml      # untuk dev / awal
# kubectl apply -f k8s/mosquitto-production.yaml   # production: ACL+auth+PVC
# kubectl apply -f k8s/mosquitto-nodeport.yaml     # bila ESP32 LAN
```

> Production: gunakan `mosquitto-production.yaml` + `mosquitto-nodeport.yaml`. Buat secret `sapa-mosquitto-auth` dari file `mosquitto_passwd`, dan UFW allow hanya subnet LAN ke port `31883`.

## 5) Deploy Backend di VPS (systemd) + env baru

```bash
sudo mkdir -p /opt/sapa && sudo chown -R deploy:deploy /opt
cd /opt && git clone <REPO_URL> sapa
cd sapa/backend && python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Buat `/opt/sapa/backend/.env`:

```
DATABASE_URL=postgresql://user:password@<POSTGRES_HOST>:5432/sapa_db
MONGODB_URI=mongodb://sapa-mongo.sapa.svc.cluster.local:27017
MONGODB_DB=sapa
MONGODB_COLLECTION_ATTENDANCE=attendance_logs
MONGODB_COLLECTION_AUDIT=audit_logs

MQTT_BROKER=sapa-mosquitto.sapa.svc.cluster.local
MQTT_PORT=1883
MQTT_USERNAME=backend
MQTT_PASSWORD=<password kuat>
MQTT_TOPIC_GATE=sapa/gate
MQTT_TOPIC_ATTENDANCE=sapa/attendance
MQTT_TOPIC_DEVICE_STATUS=sapa/device/status
MQTT_TOPIC_DEVICE_HEARTBEAT=sapa/device/heartbeat
MQTT_TOPIC_PIR=sapa/pir
IOT_OFFLINE_AFTER_SECONDS=15

EDGE_INGEST_KEY=<random>
SECRET_KEY=<random panjang>
```

Lalu unit systemd (lihat contoh sebelumnya). Restart:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sapa-backend
sudo journalctl -u sapa-backend -f
```

## Modul AI Gate

Modul `backend/ai_gate.py` adalah berkas Python independen yang berisi satu FastAPI router dengan prefix `/api/edge` plus klien `paho-mqtt` publish-only. Modul ini menjadi jembatan resmi tiga komponen SAPA: Dashboard_Web (Manager mengunggah foto wajah ke `Faces_Storage`), Edge_Laptop (menarik daftar foto referensi via `GET /api/edge/faces`, mengunduh berkas dari `/api/static/faces/{filename}`, lalu melaporkan hasil pencocokan via `POST /api/edge/face-match`), dan ESP32_Gate (menerima `{"action": "open"|"invalid", ...}` dari topic MQTT `sapa/gate`). Modul tidak meng-import `backend.main`, `backend.models`, `backend.schemas`, `backend.database`, atau `backend.mongo_db` — semua state diakses lewat klien `pymongo` dan SQLAlchemy core mandiri yang dikonfigurasi via environment variable.

Alur lifecycle foto wajah berjalan satu arah: Dashboard_Web menyimpan foto ke PVC `Faces_Storage` di backend (`backend/uploads/faces/{employee_id}.jpg`), Edge_Laptop menyinkronkan ke disk lokalnya secara periodik, lalu setiap face match valid dipublish ke topic `sapa/gate` agar ESP32_Gate menggerakkan servo. Modul `ai_gate.py` hanya melakukan `publish()` ke MQTT_Broker; subscribe untuk topic `sapa/attendance`, `sapa/device/heartbeat`, dan `sapa/pir` tetap dipegang oleh `backend/main.py` agar tidak ada dua proses yang berebut menangani pesan inbound.

### Tabel Env Variables AI Gate

| nama env | default | keterangan |
|----------|---------|------------|
| `MQTT_BROKER` | `sapa-mosquitto` | Hostname MQTT_Broker yang dipakai modul AI Gate untuk publish ke `sapa/gate`. Pada deployment k3s/kubeadm nilainya adalah service name `sapa-mosquitto.sapa.svc.cluster.local`. |
| `MQTT_PORT` | `1883` | Port TCP MQTT_Broker. Modul memvalidasi rentang 1024–65535; nilai di luar rentang atau tidak terparse akan otomatis fallback ke `1883` dengan log error ke stdout. |
| `MQTT_USERNAME` | `backend` | Username Mosquitto untuk publish topic `sapa/gate`. Nilainya disuplai dari secret `sapa-backend-secret` di namespace `sapa`. |
| `MQTT_PASSWORD` | (kosong) | Password Mosquitto pasangan `MQTT_USERNAME`. Disuplai dari secret `sapa-backend-secret`; jika kosong di production, publish akan ditolak broker dan modul mengembalikan HTTP 503 `mqtt_unavailable`. |
| `MQTT_TOPIC_GATE` | `sapa/gate` | Topic MQTT tujuan publish Gate_Command. Modul memvalidasi panjang ≤128 karakter dan pola `^[A-Za-z0-9_/+#-]+$`; nilai invalid otomatis fallback ke `sapa/gate` dengan log error ke stdout. |
| `MONGODB_URI` | `mongodb://localhost:27017` | Connection string MongoDB tempat modul menulis Attendance_Log dan Audit_Log. Pada deployment k3s/kubeadm nilainya adalah `mongodb://sapa-mongo.sapa.svc.cluster.local:27017`. |
| `MONGODB_DB` | `sapa` | Nama database MongoDB. Modul hanya mengakses koleksi `attendance_logs` (presensi sukses) dan `audit_logs` (peringatan unknown_face dan kegagalan publish MQTT); koleksi lain tidak disentuh. |

### Registrasi router di `backend/main.py`

Modul AI Gate cukup diregistrasikan dengan dua baris di `backend/main.py`:

```python
from ai_gate import router as ai_gate_router
```

```python
app.include_router(ai_gate_router)
```

Tambahkan baris `from ai_gate import …` di blok import paling atas `backend/main.py`, satu baris bersama import modul lain yang sudah ada (mis. `from auth import …`, `from database import …`). Tambahkan baris `app.include_router(ai_gate_router)` setelah baris `app = FastAPI(...)` (dan setelah `app.add_middleware(CORSMiddleware, ...)` jika sudah ada), sehingga router AI Gate ikut terdaftar saat startup. Bila import `ai_gate` melempar exception, bungkus kedua baris di blok `try/except Exception as exc` dan cetak pesan ke stdout dengan `flush=True` agar Backend_App tetap melayani endpoint lain.

### Verifikasi cepat

Setelah backend di-restart, jalankan:

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "X-EDGE-KEY: $EDGE_INGEST_KEY" \
  https://sapa.example.com/api/edge/faces
```

Harapan: respons HTTP 200 dengan body JSON `{"faces": [...]}`. Bila respons 503 `edge_auth_misconfigured`, periksa apakah secret `sapa-backend-secret` sudah berisi `EDGE_INGEST_KEY`.

## 6) Build & Deploy Frontend (Nginx)

```bash
cd /opt/sapa/frontend
npm ci
npm run build
sudo mkdir -p /var/www/sapa
sudo rsync -a --delete /opt/sapa/frontend/dist/ /var/www/sapa/
```

`/etc/nginx/sites-available/sapa` (sudah benar dari versi sebelumnya): SPA fallback `try_files $uri /index.html`, reverse proxy `/api/` → `127.0.0.1:8080`.

```bash
sudo ln -sf /etc/nginx/sites-available/sapa /etc/nginx/sites-enabled/sapa
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d sapa.example.com
```

## 7) Edge Server — AI Face Recognition di edge

Edge server adalah Python service yang **berjalan di mesin lokal dekat gate** (PC, mini-PC, RPi 4/5, Jetson). Kode-nya ada di folder `edge_server/`.

### 7.1 Bagaimana path `/edge` di VPS terhubung ke model AI di edge

- Saat halaman `/edge` di buka di **edge device** (misal PC dekat gate), browser meminta izin kamera, lalu setiap ~350 ms mengirim frame JPEG ke `POST /api/edge/frame`. Frame ini langsung dilihat di Dashboard (live preview).
- Selain browser-publisher itu, **Edge Server (Python)** menjalankan model AI lokal:
  1. Tarik daftar `GET /api/employees/` dari VPS.
  2. Tarik gambar wajah `GET /api/uploads/faces/<id>.jpg`.
  3. Hitung embedding wajah lokal → simpan di `embeddings.npz`.
  4. Untuk tiap frame kamera, cocokkan embedding live dengan cache.
  5. Bila match valid → `POST /api/edge/face-match {is_valid:true, employee_id, confidence}`.
  6. Backend mencatat log presensi di MongoDB **dan** publish `sapa/gate {action:"open", employee_id}` lewat MQTT → ESP32 buka servo + bunyi buzzer pendek.
  7. Bila wajah terdeteksi tapi tak match → `is_valid:false` → backend publish `sapa/gate {action:"invalid"}` → ESP32 bunyikan buzzer ganda, servo tetap tutup.

> Model AI tetap di edge (privasi + latensi). VPS hanya menyimpan gambar referensi & menerima keputusan.

### 7.2 Install di edge

```bash
git clone <REPO_URL> /opt/sapa
cd /opt/sapa/edge_server
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env   # isi API base, edge key, MQTT broker (LAN/VPN)
```

Linux dependencies untuk `face_recognition`:

```bash
sudo apt install -y cmake libopenblas-dev liblapack-dev libgl1
```

### 7.3 Jalankan

```bash
./venv/bin/python -m edge_server.main
```

Service akan:
- konek MQTT broker (publish heartbeat ke `sapa/device/heartbeat` setiap 5s — itu yang membuat tombol Open/Close di Dashboard menjadi **enabled** dan badge **Gate ONLINE** menyala);
- sync embedding setiap 60 detik;
- push frame 2 fps ke `/api/edge/frame`;
- match wajah lalu push hasil ke `/api/edge/face-match`.

## 8) ESP32 (servo + buzzer + PIR) — `esp32/sapa_gate.ino`

- Servo di GPIO 13, buzzer GPIO 14, PIR GPIO 27.
- Ubah `WIFI_SSID/PASS` dan `MQTT_HOST/USER/PASS` lalu flash via Arduino IDE.
- Subscribe `sapa/gate`:
  - `{"action":"open"}` → servo ke OPEN_ANGLE, beep 1×, hold sampai PIR mendeteksi pergerakan, lalu close (mirip KAI Access).
  - `{"action":"close"}` → servo ke CLOSED_ANGLE.
  - `{"action":"invalid"}` → buzzer beep 2×, servo tetap tutup.
- Publish `sapa/device/heartbeat` setiap 5s → backend tahu device online.
- Publish `sapa/pir` saat PIR HIGH → backend juga publish `close` ke gate (server-side fail-safe).

### 8.1 ESP32 LAN ke broker
Gunakan `mosquitto-nodeport.yaml` (NodePort 31883), UFW allow `192.168.0.0/24` ke port itu, MQTT_HOST = IP VPS LAN.

### 8.2 ESP32 beda jaringan
Pasang gateway WireGuard / mini router yang join VPN, ESP32 connect ke gateway via LAN; gateway forward ke broker via tunnel.

## 9) Login Audit (Username, Status, Timestamp) di VPS

- Endpoint `POST /login` sekarang menulis dokumen ke MongoDB collection `audit_logs`:
  ```json
  { "username": "manager", "status": "success", "timestamp": "2026-05-23T..." , "event_type": "login_success", ... }
  { "username": "admin",   "status": "failed",  "timestamp": "...", "event_type": "login_failed", ... }
  ```
- Endpoint `GET /audit/logins` (manager only) mengambil 200 entry terbaru. Dashboard tab **System → Login Activity** memuatnya dan auto-refresh tiap 10 detik.

Backup: `mongodump --db sapa --collection audit_logs` jadwalkan via CronJob k8s atau systemd timer.

## 10) Realtime log presensi di Dashboard

- Frontend sudah polling `GET /attendance/` setiap 5 detik → log update otomatis.
- Backend menulis tiap face match valid (dari edge) ke MongoDB `attendance_logs` dengan field `source: edge_ai|edge_mqtt|manual`, `confidence`.
- Tab Logs Dashboard punya filter (date / direction / status / employee) + ekspor CSV.

## 11) Verifikasi end-to-end

1. **Login audit**: login dengan kredensial salah lalu benar; cek di tab System → Login Activity.
2. **IoT status**: matikan ESP32 selama >15s. Badge berubah ke **Gate OFFLINE** dan tombol Open/Close ter-disable. Hidupkan kembali → ONLINE.
3. **Manual gate**: pencet Open/Close (saat ONLINE). ESP32 servo bergerak, buzzer beep.
4. **Edge live preview**: buka `/edge` di edge device, dashboard menampilkan frame.
5. **Face match**: jalankan edge server. Arahkan wajah employee terdaftar. Servo membuka, log baru muncul realtime di dashboard. Wajah asing → buzzer ganda, log tidak bertambah.
6. **PIR auto-close**: setelah gate dibuka, lewatkan tangan di depan PIR — servo menutup dan tab System menampilkan `last_action: auto_close_pir`.
7. **View Faces**: di tab Employees, klik **View Faces** → modal menampilkan foto referensi `/uploads/faces/<id>.jpg`.

## 12) Hardening

- Set `EDGE_INGEST_KEY` panjang dan rahasiakan; edge server kirimkan via field `edge_key` / header `X-EDGE-KEY`.
- Mosquitto: `allow_anonymous false`, `password_file`, `acl_file` untuk membatasi user `backend` (publish `sapa/gate`, subscribe sisanya) dan `esp32` (subscribe `sapa/gate`, publish `sapa/device/heartbeat`, `sapa/pir`).
- WireGuard untuk ESP32 jika berbeda jaringan.
- HTTPS wajib agar `/edge` (browser) bisa minta izin kamera (`getUserMedia`).
- Backup MongoDB & PostgreSQL terjadwal.

## 13) Troubleshooting cepat

| Gejala | Periksa |
|--------|---------|
| Tombol Open/Close ter-disable | Edge/ESP32 tidak publish heartbeat. `sudo journalctl -u sapa-backend -f` lihat apakah on_message dapat MQTT. |
| Live frame kosong | `/edge` di edge device tidak terbuka, atau backend tidak menerima `/edge/frame` (cek Nginx access log + `EDGE_INGEST_KEY`). |
| Wajah employee tak dikenali | Edge server belum sync (`./venv/bin/python -m edge_server.main` dan tunggu sync log). Threshold terlalu ketat: turunkan `RECOGNITION_THRESHOLD`. Foto referensi tidak ada wajah jelas: re-upload dari Add Employee. |
| Login audit kosong | MongoDB tidak terjangkau. `kubectl -n sapa get pods` & `MONGODB_URI` di backend `.env`. |
| Servo tetap diam saat valid | Cek topic match: `mosquitto_sub -h <broker> -t sapa/gate -v` saat scan wajah. |

