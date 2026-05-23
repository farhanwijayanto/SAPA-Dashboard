# SAPA — Panduan Deployment dari Nol (GitHub → VPS Ubuntu + kubeadm → Laptop Edge → ESP32)

Domain target: **`sapa.farhn.dev`**
Skema akhir:

```
┌──────────────┐  HTTPS  ┌────────────────────────────────────────────┐
│ Browser      │────────▶│ VPS Ubuntu Server (Kubernetes - kubeadm)   │
│ admin/manager│         │  ├ ingress-nginx :80/:443                   │
└──────────────┘         │  │   ├ /          → svc sapa-frontend       │
                         │  │   └ /api/*     → svc sapa-backend (FastAPI)
                         │  ├ svc sapa-db (PostgreSQL)                 │
                         │  ├ svc sapa-mongo (MongoDB)                 │
                         │  └ svc sapa-mosquitto :1883 + NodePort 31883│
                         └────────────────────────┬───────────────────┘
                                                  │ MQTT 31883 + HTTPS /api
                                                  │
                          ┌───────────────────────┴───────────────────┐
                          ▼                                           ▼
                 ┌────────────────────┐                       ┌──────────────┐
                 │ LAPTOP (EDGE)      │                       │ ESP32 (Gate) │
                 │ - kamera laptop    │                       │ servo+buzzer │
                 │ - AI face_recognition (lokal)              │ +PIR         │
                 │ - browser /edge live preview               │              │
                 │ - publish heartbeat ke MQTT 31883          │ MQTT 31883   │
                 │ - POST /api/edge/face-match → backend     │              │
                 └────────────────────┘                       └──────────────┘
```

Laptop (= edge) adalah satu-satunya yang menjalankan AI dan kamera. Laptop terhubung ke VPS via internet (HTTPS + MQTT TCP 31883) dan ke ESP32 secara tidak langsung lewat broker MQTT yang ada di VPS. Browser admin/manager bisa dibuka di mana saja (HP, PC kantor, dll.) selama bisa akses `https://sapa.farhn.dev`.

---

## 0) Yang perlu disiapkan

| Item                     | Spesifikasi minimum                                            |
|--------------------------|----------------------------------------------------------------|
| VPS Ubuntu               | Ubuntu 22.04 LTS, 2 vCPU / 4 GB RAM / 40 GB disk, IP publik    |
| Domain                   | `sapa.farhn.dev` di-A-record-kan ke IP VPS                     |
| Akun GitHub              | untuk hosting repo                                             |
| Laptop (edge)            | Ubuntu 22.04/Windows + WSL2/Linux mint, webcam, RAM ≥ 4 GB     |
| ESP32                    | servo, buzzer, PIR (sudah ada di `esp32/sapa_gate.ino`)        |
| Tools di Windows lokal   | Git for Windows                                                |

---

## 1) Upload project dari Windows lokal ke GitHub

> Folder kerja Anda: `C:\Users\farha\Documents\trae_projects\sapa1.2`. Repo ini sudah punya `.gitignore` yang mengecualikan `venv`, `node_modules`, `dist`, `.env*`, `attendance.db`, `backend/uploads/faces/`, dan `k8s/secrets.yaml` — aman langsung di-commit.

Buka `cmd` (atau PowerShell) di Windows:

```bat
:: 1.1 cek git
git --version

:: 1.2 masuk ke folder project
cd /d C:\Users\farha\Documents\trae_projects\sapa1.2

:: 1.3 inisialisasi git, set branch main, commit awal
git init
git branch -M main
git add .
git commit -m "Initial SAPA 1.2"

:: 1.4 buka https://github.com → New repository
::      Nama: sapa
::      Visibility: Public/Private (terserah)
::      JANGAN centang README/license/.gitignore (kita sudah punya)
::      Klik Create repository, salin URL HTTPS-nya, contoh:
::        https://github.com/farhn/sapa.git

:: 1.5 hubungkan & push
git remote add origin https://github.com/<USER>/sapa.git
git push -u origin main
```

Jika `git push` minta password:
- pilih opsi **Personal Access Token (classic)** dari `GitHub → Settings → Developer settings → Tokens (classic) → Generate`. Centang scope `repo`, salin tokennya, pakai sebagai password saat `git push`.
- atau pakai SSH: `git remote set-url origin git@github.com:<USER>/sapa.git`, lalu tambahkan public key (`~/.ssh/id_ed25519.pub`) ke `GitHub → SSH keys`.

Update kode setelah ini cukup:
```bat
git add .
git commit -m "perubahan ..."
git push
```

---

## 2) Persiapan VPS Ubuntu Server

SSH ke VPS:
```bash
ssh root@<IP_VPS>      # atau user lain dengan sudo
```

Semua command di bawah dijalankan di **VPS**.

```bash
# 2.1 update OS + tools
sudo apt update && sudo apt -y upgrade
sudo apt install -y curl wget gnupg ca-certificates apt-transport-https \
  software-properties-common ufw git jq mosquitto-clients

# 2.2 hostname
sudo hostnamectl set-hostname sapa-cp
echo "$(hostname -I | awk '{print $1}') sapa-cp" | sudo tee -a /etc/hosts

# 2.3 firewall (sebelum install kubeadm)
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp        # publik (frontend + /api)
sudo ufw allow 443/tcp       # publik (HTTPS)
sudo ufw allow 6443/tcp      # kube-apiserver (worker join)
sudo ufw allow 10250/tcp     # kubelet
sudo ufw allow 31883/tcp     # MQTT NodePort untuk laptop edge & ESP32
sudo ufw allow 8472/udp      # flannel VXLAN
sudo ufw --force enable

# 2.4 swap WAJIB off (kubeadm requirement)
sudo swapoff -a
sudo sed -i '/ swap / s/^\(.*\)$/#\1/g' /etc/fstab

# 2.5 module kernel + sysctl
sudo tee /etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF
sudo modprobe overlay
sudo modprobe br_netfilter

sudo tee /etc/sysctl.d/k8s.conf <<EOF
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sudo sysctl --system
```

---

## 3) Clone repo SAPA dari GitHub ke VPS

```bash
# 3.1 siapkan lokasi
sudo mkdir -p /opt && sudo chown $USER:$USER /opt

# 3.2 clone
git clone https://github.com/<USER>/sapa.git /opt/sapa
cd /opt/sapa
ls
```

Setelah ini semua perintah Kubernetes & Docker dijalankan dari `/opt/sapa`.

---

## 4) Install Kubernetes dengan kubeadm di VPS

### 4.1 Pasang containerd

```bash
sudo apt install -y containerd
sudo mkdir -p /etc/containerd
containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sudo systemctl restart containerd
sudo systemctl enable containerd
```

### 4.2 Pasang kubeadm, kubelet, kubectl (versi 1.30)

```bash
sudo mkdir -p -m 755 /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.30/deb/Release.key \
  | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg

echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.30/deb/ /' \
  | sudo tee /etc/apt/sources.list.d/kubernetes.list

sudo apt update
sudo apt install -y kubelet kubeadm kubectl
sudo apt-mark hold kubelet kubeadm kubectl
```

### 4.3 Init control-plane (single-node)

```bash
sudo kubeadm init \
  --pod-network-cidr=10.244.0.0/16 \
  --apiserver-advertise-address=$(hostname -I | awk '{print $1}')

# kubectl untuk user biasa
mkdir -p $HOME/.kube
sudo cp -i /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config

# karena single-node, izinkan pod schedule di control-plane
kubectl taint nodes --all node-role.kubernetes.io/control-plane- || true

kubectl get nodes
```

### 4.4 Pasang CNI Flannel

```bash
kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
kubectl get pods -n kube-flannel -w
# tekan Ctrl+C kalau semua pod sudah Running
```

### 4.5 Pasang Ingress NGINX (controller publik untuk domain)

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/baremetal/deploy.yaml

# expose via NodePort tetap di 30080/30443
kubectl -n ingress-nginx patch svc ingress-nginx-controller \
  -p '{"spec":{"type":"NodePort","ports":[{"name":"http","port":80,"targetPort":80,"nodePort":30080,"protocol":"TCP"},{"name":"https","port":443,"targetPort":443,"nodePort":30443,"protocol":"TCP"}]}}'

kubectl -n ingress-nginx get pods -w
# tunggu controller Running
```

Forward port 80/443 publik → NodePort 30080/30443 (iptables):

```bash
sudo iptables -t nat -A PREROUTING -p tcp --dport 80  -j REDIRECT --to-port 30080
sudo iptables -t nat -A PREROUTING -p tcp --dport 443 -j REDIRECT --to-port 30443
sudo apt install -y iptables-persistent
sudo netfilter-persistent save
```

### 4.6 Pasang cert-manager (TLS Let's Encrypt)

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
kubectl -n cert-manager get pods -w
# tekan Ctrl+C kalau ketiga pod Running
```

---

## 5) Build image SAPA di VPS

```bash
sudo apt install -y docker.io
sudo usermod -aG docker $USER && newgrp docker

cd /opt/sapa
docker build -t sapa-backend:1.2  ./backend
docker build -t sapa-frontend:1.2 ./frontend

# import ke containerd k8s namespace agar pod bisa pakai image lokal tanpa registry
docker save sapa-backend:1.2  | sudo ctr -n k8s.io images import -
docker save sapa-frontend:1.2 | sudo ctr -n k8s.io images import -

sudo ctr -n k8s.io images list | grep sapa-
```

> Image tag `sapa-backend:1.2` & `sapa-frontend:1.2` sudah dipakai di manifest (dengan `imagePullPolicy: IfNotPresent`).

---

## 6) Deploy stack SAPA ke Kubernetes

### 6.1 Buat namespace + Secret

```bash
cd /opt/sapa
kubectl apply -f k8s/namespace.yaml

# 6.1.1 Postgres
kubectl -n sapa create secret generic sapa-db-secret \
  --from-literal=POSTGRES_USER=sapa \
  --from-literal=POSTGRES_PASSWORD=$(openssl rand -hex 16) \
  --from-literal=POSTGRES_DB=sapa_db

# baca lagi password yg digenerate untuk DATABASE_URL
PG_USER=$(kubectl -n sapa get secret sapa-db-secret -o jsonpath='{.data.POSTGRES_USER}' | base64 -d)
PG_PASS=$(kubectl -n sapa get secret sapa-db-secret -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)
PG_DB=$(kubectl  -n sapa get secret sapa-db-secret -o jsonpath='{.data.POSTGRES_DB}'       | base64 -d)

# 6.1.2 Backend (DB + Mongo + JWT + Edge ingest key)
kubectl -n sapa create secret generic sapa-backend-secret \
  --from-literal=DATABASE_URL="postgresql://${PG_USER}:${PG_PASS}@sapa-db:5432/${PG_DB}" \
  --from-literal=MONGODB_URI="mongodb://sapa-mongo:27017" \
  --from-literal=SECRET_KEY=$(openssl rand -hex 32) \
  --from-literal=EDGE_INGEST_KEY=$(openssl rand -hex 24) \
  --from-literal=MQTT_USERNAME="" \
  --from-literal=MQTT_PASSWORD=""
```

### 6.2 Buat password Mosquitto + Secret broker

```bash
cd /opt/sapa

# tiga user: backend (in-cluster FastAPI), edge (laptop), esp32 (gate)
mosquitto_passwd -c -b passwords backend $(openssl rand -hex 12)
mosquitto_passwd -b   passwords edge    $(openssl rand -hex 12)
mosquitto_passwd -b   passwords esp32   $(openssl rand -hex 12)

# CATAT password plaintext (Anda perlu untuk konfigurasi laptop & ESP32)
cat passwords

kubectl -n sapa create secret generic sapa-mosquitto-auth \
  --from-file=passwords=./passwords
rm -f passwords
```

> Password plaintext perlu Anda simpan secara aman. Cara mudah: tulis di password manager dengan label `sapa-mqtt-edge`, `sapa-mqtt-esp32`, `sapa-mqtt-backend`.

### 6.3 Apply seluruh stack sekaligus

```bash
kubectl apply -k k8s/
```

`kustomization.yaml` apply: namespace, db (Postgres + PVC), mongo (Mongo + PVC), mosquitto-production (auth + ACL + PVC), mosquitto-nodeport (port `31883`), backend (FastAPI + PVC uploads), frontend (Nginx + React), ingress (`sapa.farhn.dev` + TLS).

### 6.4 Update Secret backend dengan kredensial MQTT

Setelah Mosquitto siap, masukkan kredensial `backend` ke Secret backend lalu restart:

```bash
MQTT_BACKEND_PW='<password backend dari step 6.2>'

kubectl -n sapa patch secret sapa-backend-secret --type merge -p "$(jq -n \
  --arg u backend --arg p "$MQTT_BACKEND_PW" \
  '{stringData:{MQTT_USERNAME:$u, MQTT_PASSWORD:$p}}')"

kubectl -n sapa rollout restart deploy/sapa-backend
```

### 6.5 Cek

```bash
kubectl -n sapa get pods,svc,pvc,ingress -o wide
```

Semua pod harus `Running`. Khusus `sapa-backend` butuh waktu ~30 detik karena `entrypoint.sh` menjalankan `python -m backend.seed` (membuat user awal admin/manager).

---

## 7) DNS + sertifikat HTTPS untuk `sapa.farhn.dev`

### 7.1 DNS

Di panel DNS Anda untuk domain `farhn.dev` tambahkan:

| Type | Name | Value         | TTL  |
|------|------|---------------|------|
| A    | sapa | `<IP_VPS>`    | Auto |

Bila pakai Cloudflare, set proxy **DNS only** dulu (awan abu-abu) supaya HTTP-01 challenge bisa lewat. Setelah cert terbit baru aktifkan proxy.

### 7.2 Pasang ClusterIssuer Let's Encrypt

```bash
kubectl apply -f k8s/cluster-issuer.yaml
```

Manifest `k8s/ingress.yaml` sudah memakai annotation `cert-manager.io/cluster-issuer: letsencrypt-prod` dan host `sapa.farhn.dev`, jadi sertifikat akan otomatis diterbitkan setelah DNS A record menyebar.

```bash
kubectl -n sapa describe certificate sapa-farhn-dev-tls
# tunggu sampai Status: Ready=True
```

### 7.3 Tes

```bash
curl -I https://sapa.farhn.dev/
curl -I https://sapa.farhn.dev/api/employees/
```

Buka di browser:
- Admin/manager: `https://sapa.farhn.dev/` (login default ada di hasil seeder backend)
- Halaman edge nanti: `https://sapa.farhn.dev/edge`

---

## 8) Setup Laptop (Edge) — kamera + AI face recognition + jembatan IoT

> **Penting**: laptop yang akan dipasang dekat gate adalah satu-satunya tempat AI berjalan. VPS tidak menyimpan model atau melakukan inferensi. Browser `/edge` di laptop juga yang menyediakan live preview ke dashboard.

Semua langkah di bawah dijalankan **di laptop** (Ubuntu/WSL2 disarankan; Windows native bisa tapi `face_recognition` lebih mudah di Linux).

### 8.1 Install dependency sistem

Ubuntu / WSL2:
```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip cmake \
  libopenblas-dev liblapack-dev libgl1 git
```

Windows native: pasang Python 3.11 dari python.org, lalu Visual Studio Build Tools (C++ workload + CMake) supaya `dlib` bisa di-build.

### 8.2 Clone repo + buat virtualenv

```bash
git clone https://github.com/<USER>/sapa.git ~/sapa
cd ~/sapa/edge_server

python3.11 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

> `requirements.txt` edge berisi `opencv-python`, `face-recognition` (butuh dlib + cmake), `paho-mqtt`, `requests`, `python-dotenv`. Build pertama bisa 5-10 menit karena dlib di-compile.

### 8.3 Ambil token dan kunci dari VPS

Dari laptop (atau dari shell VPS lalu kirim hasilnya ke laptop):

```bash
# 8.3.1 token JWT manager (login default seeder; ganti password setelah login pertama)
curl -s -X POST https://sapa.farhn.dev/api/login \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode username=manager \
  --data-urlencode password=manager \
  | jq -r .access_token
# salin output sebagai SAPA_API_TOKEN

# 8.3.2 EDGE_INGEST_KEY (jalankan DI VPS, lalu salin)
kubectl -n sapa get secret sapa-backend-secret \
  -o jsonpath='{.data.EDGE_INGEST_KEY}' | base64 -d; echo
```

### 8.4 Isi `.env` edge

```bash
cd ~/sapa/edge_server
cp .env.example .env
nano .env
```

Isi:

```
SAPA_API_BASE=https://sapa.farhn.dev/api
SAPA_API_TOKEN=<token JWT manager dari 8.3.1>
EDGE_INGEST_KEY=<EDGE_INGEST_KEY dari 8.3.2>

MQTT_BROKER=sapa.farhn.dev
MQTT_PORT=31883
MQTT_USERNAME=edge
MQTT_PASSWORD=<password user 'edge' yang Anda generate di 6.2>

MQTT_TOPIC_GATE=sapa/gate
MQTT_TOPIC_HEARTBEAT=sapa/device/heartbeat
MQTT_TOPIC_PIR=sapa/pir

CAMERA_INDEX=0
RECOGNITION_THRESHOLD=0.55
COOLDOWN_SECONDS=5
SYNC_EVERY_SECONDS=60
FRAME_PUSH_FPS=2
```

### 8.5 Jalankan service edge AI

Test dulu manual:
```bash
cd ~/sapa
./edge_server/venv/bin/python -m edge_server.main
# Anda akan melihat log:
#   MQTT connected rc=0
#   Synced N employee faces -> N embeddings
#   Edge server running. threshold=0.55 cooldown=5s
```

Bila stabil, jadikan systemd service (Ubuntu/WSL2):

```bash
sudo tee /etc/systemd/system/sapa-edge.service >/dev/null <<EOF
[Unit]
Description=SAPA Edge AI (laptop)
After=network-online.target

[Service]
WorkingDirectory=$HOME/sapa
EnvironmentFile=$HOME/sapa/edge_server/.env
ExecStart=$HOME/sapa/edge_server/venv/bin/python -m edge_server.main
Restart=always
RestartSec=3
User=$USER

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now sapa-edge
sudo journalctl -u sapa-edge -f
```

### 8.6 Buka halaman `/edge` di laptop

Buka **Chromium / Chrome / Edge** di laptop:
```
https://sapa.farhn.dev/edge
```

Halaman akan minta izin kamera (HTTPS wajib agar `getUserMedia` bisa). Setelah diizinkan, browser mulai mengirim frame ke `POST /api/edge/frame` setiap ~350 ms — frame ini yang muncul sebagai live preview di Dashboard admin.

> **Pembagian tugas di laptop:**
> - **Browser `/edge`** = mengambil frame kamera dan dikirim ke VPS untuk preview di dashboard.
> - **Service `sapa-edge` (Python)** = melakukan inferensi face_recognition lokal, dan kalau match → POST ke `/api/edge/face-match` + publish heartbeat ke MQTT.
>
> Keduanya berjalan paralel di laptop yang sama. Laptop menjadi jembatan: dashboard ↔ AI ↔ MQTT (yang lalu sampai ke ESP32).

### 8.7 Cek dari sisi VPS

```bash
# heartbeat dari laptop terlihat di broker
mosquitto_sub -h sapa.farhn.dev -p 31883 -u edge -P '<password edge>' \
  -t 'sapa/device/heartbeat' -v

# Dashboard manager → tab System → badge "Gate ONLINE" + tombol Open/Close enabled
# Dashboard manager → tab System → Live Camera → menampilkan frame dari /edge
```

---

## 9) Flash & koneksikan ESP32 ke VPS

Buka `esp32/sapa_gate.ino` di Arduino IDE:

```cpp
const char* WIFI_SSID = "wifi-anda";
const char* WIFI_PASS = "password-wifi";
const char* MQTT_HOST = "sapa.farhn.dev";   // bisa juga IP publik VPS
const uint16_t MQTT_PORT = 31883;
const char* MQTT_USER = "esp32";
const char* MQTT_PASS = "<password user 'esp32' dari 6.2>";
```

Flash, lalu di Serial Monitor pastikan:
```
WiFi connected
MQTT connected
heartbeat sent
```

Topik yang ESP32 perlu sentuh sudah di-allow di ACL Mosquitto:
- subscribe: `sapa/gate`
- publish: `sapa/device/heartbeat`, `sapa/device/status`, `sapa/pir`, `sapa/attendance`

---

## 10) Verifikasi end-to-end

Skenario sukses (semuanya jalan setelah §8 dan §9):

1. **Live preview** — buka `https://sapa.farhn.dev/edge` di laptop → di dashboard manager (PC kantor) tab System tampak gambar kamera laptop realtime.
2. **Heartbeat** — laptop edge & ESP32 sama-sama publish `sapa/device/heartbeat`. Badge dashboard: **Gate ONLINE**, tombol Open/Close enabled.
3. **Manual gate** — klik **Open** di dashboard → backend publish `sapa/gate {action:"open"}` → ESP32 servo membuka, buzzer beep 1×.
4. **AI face recognition (laptop)** — arahkan wajah employee ke kamera laptop. `sapa-edge.service` mengenali → POST `/api/edge/face-match` → backend publish `sapa/gate {action:"open", employee_id}` ke MQTT → ESP32 servo membuka. Log presensi muncul realtime di dashboard.
5. **Wajah asing** — `sapa-edge.service` POST `is_valid:false` → backend publish `sapa/gate {action:"invalid"}` → ESP32 buzzer ganda, servo tetap tutup.
6. **PIR auto-close** — setelah gate dibuka, ESP32 publish `sapa/pir {motion:true}` → backend publish `close` → servo tutup, dashboard `last_action: auto_close_pir`.
7. **Login audit** — login salah/benar di dashboard tersimpan di MongoDB collection `audit_logs` dan tampil di tab System → Login Activity.

Quick check dari VPS:
```bash
# konektivitas in-cluster
kubectl -n sapa exec deploy/sapa-backend -- python - <<'PY'
import socket
for h,p in [("sapa-db",5432),("sapa-mongo",27017),("sapa-mosquitto",1883)]:
    s=socket.socket(); s.settimeout(2)
    try: s.connect((h,p)); print(h,"OK")
    except Exception as e: print(h,"FAIL",e)
PY

# log backend
kubectl -n sapa logs -f deploy/sapa-backend
# log mosquitto (lihat siapa saja yang connect/disconnect)
kubectl -n sapa logs -f deploy/sapa-mosquitto
```

---

## 11) Operasi & troubleshooting cepat

| Gejala | Periksa |
|--------|---------|
| Pod backend `CrashLoopBackOff` | `kubectl -n sapa logs deploy/sapa-backend`. Biasanya `DATABASE_URL` salah atau Postgres belum siap. |
| Tombol Open/Close disabled | Heartbeat tidak masuk. Cek `kubectl -n sapa logs deploy/sapa-mosquitto` & service `sapa-edge` di laptop. |
| Live preview kosong | Browser `/edge` tidak terbuka di laptop, atau `EDGE_INGEST_KEY` di laptop ≠ Secret di VPS. |
| Wajah tak dikenali | Tunggu sync (`Synced N embeddings` di log laptop). Foto employee tidak ada wajah jelas → re-upload. Turunkan `RECOGNITION_THRESHOLD`. |
| ESP32 tidak konek MQTT | Password user `esp32` salah, atau UFW VPS belum allow 31883 dari IP publik ISP ESP32. |
| Cert HTTPS belum keluar | DNS A record belum propagate, atau Cloudflare proxy aktif. Set ke "DNS only" lalu `kubectl -n sapa describe certificate sapa-farhn-dev-tls`. |
| Mau update kode | Di Windows: edit → `git push`. Di VPS: `cd /opt/sapa && git pull` lalu rebuild image (lihat §12). |

---

## 12) Update kode setelah deploy pertama

```bash
# di VPS
cd /opt/sapa
git pull

docker build -t sapa-backend:1.3 ./backend
docker build -t sapa-frontend:1.3 ./frontend
docker save sapa-backend:1.3  | sudo ctr -n k8s.io images import -
docker save sapa-frontend:1.3 | sudo ctr -n k8s.io images import -

kubectl -n sapa set image deploy/sapa-backend  backend=sapa-backend:1.3
kubectl -n sapa set image deploy/sapa-frontend frontend=sapa-frontend:1.3
kubectl -n sapa rollout status deploy/sapa-backend
```

```bash
# di laptop
cd ~/sapa
git pull
sudo systemctl restart sapa-edge
```

Selesai. Dengan urutan ini Anda mulai dari upload Windows → GitHub, clone ke VPS Ubuntu, install Kubernetes lewat kubeadm, deploy seluruh stack SAPA (frontend + backend + Postgres + MongoDB + Mosquitto), domain `https://sapa.farhn.dev` siap pakai, laptop jadi edge AI yang jembatani dashboard ↔ ESP32 (IoT) lewat broker MQTT yang ada di VPS.
