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
git remote add origin https://github.com/farhanwijayanto/SAPA-Dashboard.git
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

# 2.4 swap → JANGAN dimatikan untuk VPS RAM kecil/dinamis.
#     Sebaliknya: pastikan swap aktif sebagai cushion (cegah VPS reboot/OOM).
#     Cek swap saat ini:
swapon --show
free -h

# Bila belum ada swap, buat swap file 4 GB (aman, idempotent):
if ! sudo swapon --show | grep -q swapfile; then
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi
sudo sysctl vm.swappiness=10
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf

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
git clone https://github.com/farhanwijayanto/SAPA-Dashboard.git /opt/sapa
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

> **VPS RAM kecil / dinamis (burstable)?** Bila kubeadm preflight memunculkan
> `[ERROR Mem]: the system RAM (xxx MB) is less than the minimum 1700 MB`,
> ikuti perintah **alternatif** di bawah. File konfigurasi `k8s/kubeadm-config.yaml`
> sudah menyertakan: kubelet `failSwapOn: false`, swap `LimitedSwap`,
> `systemReserved`/`kubeReserved` 256 Mi, dan eviction threshold supaya kernel
> tidak OOM-kill `sshd`/`kubelet` saat tertekan.

```bash
# OPSI A — VPS dengan RAM ≥ 2 GB (preflight lolos)
sudo kubeadm init \
  --pod-network-cidr=10.244.0.0/16 \
  --apiserver-advertise-address=$(hostname -I | awk '{print $1}')

# OPSI B — VPS RAM kecil / dinamis (burstable). Pakai config + bypass preflight Mem.
sudo kubeadm init \
  --config /opt/sapa/k8s/kubeadm-config.yaml \
  --ignore-preflight-errors=Mem,Swap
```

Salah satu opsi saja yang dipilih. Setelah init sukses:

```bash
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

> Catatan: rule ini dipersist dengan `iptables-persistent` (`netfilter-persistent save`). **Jangan** menjalankan `iptables -F` atau `iptables -t nat -F` setelahnya — selain memutus SSH (lihat §11.1), rule REDIRECT ini juga ikut hilang sehingga `sapa.farhn.dev` tidak bisa diakses dari publik.

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
sudo usermod -aG docker $USER

# Aktifkan group `docker` di shell saat ini. Pilih salah satu:
#   a) tutup terminal lalu ssh masuk lagi (paling sederhana)
#   b) `exec sg docker -c bash`   (tetap di sesi yang sama)
#   c) jalankan command docker dengan `sudo` selama belum logout
exec sg docker -c bash

cd /opt/sapa
docker build -t sapa-backend:1.2  ./backend
docker build -t sapa-frontend:1.2 ./frontend

#docker run jalan tapi docker build tidak. Ini bug klasik legacy docker builder: container build kadang dibuat sebelum chain DOCKER final, jadi paket di-DROP. Solusi paling cepat: paksa build pakai network host.
docker build --network=host -t sapa-backend:1.2 ./backend
docker build --network=host -t sapa-frontend:1.2 ./frontend


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

### 8.A Setup Edge di Windows 10/11

Sub-bab ini adalah jalur instalasi alternatif bagi pembaca yang tidak ingin memasang Ubuntu/WSL2 dan memilih menjalankan `edge_server` Python service langsung di **Windows 10 (versi 21H2 atau lebih baru) atau Windows 11 native**. Pilih jalur ini ATAU jalur Linux/WSL2 di §8.1–§8.5; jangan dijalankan paralel di host yang sama.

#### 8.A.0 Topologi koneksi Edge Windows ↔ VPS ↔ ESP32

Sebelum mulai instalasi, pahami bagaimana laptop Windows yang menjadi Edge_Laptop terhubung ke dua sisi: ke **VPS** (Backend_App + MQTT_Broker) dan **secara tidak langsung** ke **ESP32** (gate hardware) lewat broker MQTT yang berjalan di VPS.

```
┌──────────────────────────┐                              ┌────────────────────────────────┐
│ EDGE LAPTOP (Windows)    │                              │ VPS Ubuntu (kubeadm)           │
│  - Webcam + face_recog   │                              │                                │
│  - sapa-edge service     │                              │  ┌────────────────────────┐    │
│                          │                              │  │ Ingress NGINX          │    │
│  ┌────────────────────┐  │   HTTPS 443                  │  │  /api/* → Backend      │    │
│  │ sapa_client.py     │──┼─────────────────────────────▶│  │  /     → Frontend UI   │    │
│  │  - GET /api/edge/  │  │   (faces sync, face-match)   │  └────────────┬───────────┘    │
│  │    faces           │  │                              │               │                │
│  │  - GET /api/static/│  │                              │  ┌────────────▼───────────┐    │
│  │    faces/{file}    │  │                              │  │ Backend (FastAPI)      │    │
│  │  - POST /api/edge/ │  │                              │  │  ai_gate.py            │    │
│  │    face-match      │  │                              │  │  publish sapa/gate     │    │
│  └────────────────────┘  │                              │  └────────────┬───────────┘    │
│                          │                              │               │                │
│  ┌────────────────────┐  │   MQTT TCP 31883             │  ┌────────────▼───────────┐    │
│  │ paho-mqtt client   │──┼─────────────────────────────▶│  │ Mosquitto broker       │    │
│  │  user=edge         │  │   publish heartbeat / pir    │  │  user backend/edge/    │    │
│  │  publish:          │  │                              │  │       esp32 (ACL)      │    │
│  │  - sapa/device/    │  │                              │  └────────────┬───────────┘    │
│  │    heartbeat       │  │                              │               │ NodePort 31883 │
│  │  - sapa/pir        │  │                              │               │                │
│  │  subscribe:        │  │                              │               │                │
│  │  - sapa/gate (op)  │◀─┼──────────────────────────────┼───────────────┘                │
│  └────────────────────┘  │   gate command flowback      │               ▲                │
└──────────────────────────┘                              └───────────────│────────────────┘
                                                                          │ MQTT TCP 31883
                                                                          │
                                                          ┌───────────────│────────────────┐
                                                          │ ESP32 (Gate hardware)          │
                                                          │  WiFi → MQTT broker            │
                                                          │  user=esp32                    │
                                                          │  subscribe:                    │
                                                          │  - sapa/gate {action: open}    │
                                                          │  publish:                      │
                                                          │  - sapa/device/heartbeat       │
                                                          │  - sapa/pir                    │
                                                          │  servo + buzzer                │
                                                          └────────────────────────────────┘
```

**Tiga jalur komunikasi yang harus jalan:**

| # | Sumber → Tujuan                       | Protokol / port                       | Tujuan teknis                                                                                                                                                       |
|---|---------------------------------------|---------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 1 | Edge laptop → VPS Backend             | HTTPS TCP 443 ke `sapa.farhn.dev`     | Pull daftar wajah (`GET /api/edge/faces`), download foto referensi (`GET /api/static/faces/...`), kirim hasil match (`POST /api/edge/face-match`). Auth: header `X-EDGE-KEY`. |
| 2 | Edge laptop → VPS Mosquitto           | MQTT TCP 31883 ke `sapa.farhn.dev`    | Publish heartbeat + PIR data, subscribe gate command (`sapa/gate`) untuk audit. Auth: user `edge` + password Mosquitto.                                              |
| 3 | ESP32 → VPS Mosquitto                 | MQTT TCP 31883 ke `sapa.farhn.dev`    | ESP32 subscribe `sapa/gate`; saat Backend/Edge publish `{"action":"open"}` ESP32 menggerakkan servo. ESP32 juga publish heartbeat. Auth: user `esp32`.                |

**Catatan penting**: Edge laptop dan ESP32 **tidak pernah terhubung langsung** satu sama lain. Semua interaksi gate command dilakukan lewat MQTT broker yang berjalan di VPS. Kalau MQTT broker mati atau salah satu jalur (1/2/3) putus, gate tidak buka. Karena itu Edge laptop dan VPS harus selalu bisa saling reach lewat **dua port** (443 untuk HTTPS dan 31883 untuk MQTT).

**Konfigurasi yang dipakai untuk setiap jalur:**

| Jalur | Variabel `.env` (Edge laptop)                                                  | Sumber nilai di VPS                                              |
|-------|--------------------------------------------------------------------------------|------------------------------------------------------------------|
| 1     | `SAPA_API_BASE`, `SAPA_API_TOKEN`, `EDGE_INGEST_KEY`                           | Login manager + Secret `sapa-backend-secret` (§6.1.2)            |
| 2     | `MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME=edge`, `MQTT_PASSWORD`              | Password user `edge` dari `mosquitto_passwd` (§6.2)              |
| 3     | tidak ada di laptop — di-flash di `esp32/sapa_gate.ino`                        | Password user `esp32` dari `mosquitto_passwd` (§6.2 & §9)        |

Setelah memahami topologi ini, lanjut ke prasyarat Windows.

#### 8.A.1 Prasyarat Windows

- Windows 10 versi 21H2/22H2 atau Windows 11 (semua edisi: Home/Pro/Enterprise) — cek dengan `winver` dari kotak Run.
- RAM ≥ 4 GB (8 GB direkomendasikan agar `dlib`/`face_recognition` lancar saat enrollment 50+ pegawai).
- Webcam internal atau USB yang sudah diizinkan di **Settings → Privacy & security → Camera → Camera access = On** dan **Let desktop apps access your camera = On**. Tanpa izin ini, OpenCV `cv2.VideoCapture` mengembalikan frame kosong walau kamera fisik aktif.
- Koneksi internet stabil ke `https://sapa.farhn.dev` (TCP 443) dan ke MQTT broker SAPA di NodePort `31883` (TCP). Bila laptop berada di belakang firewall korporat, minta admin jaringan membuka egress ke kedua port tersebut.
- Hak akses **Administrator** lokal pada laptop — dibutuhkan oleh `winget` untuk memasang Visual Studio Build Tools dan oleh `nssm` untuk mendaftarkan Windows service.

#### 8.A.2 Instalasi tools via winget

Buka **PowerShell sebagai Administrator** lalu jalankan empat perintah `winget` berikut secara berurutan. Setiap perintah akan mengunduh installer dan menyelesaikan instalasi tanpa interaksi tambahan; biarkan PowerShell berjalan hingga prompt kembali sebelum melanjutkan ke perintah berikutnya.

```powershell
# Python 3.11 (versi yang dipakai venv edge)
winget install --id Python.Python.3.11 -e --accept-source-agreements --accept-package-agreements

# Git for Windows (untuk clone & pull repo)
winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements

# Visual C++ Build Tools — wajib untuk kompilasi dlib di Windows
winget install --id Microsoft.VisualStudio.2022.BuildTools -e --accept-source-agreements --accept-package-agreements `
  --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --quiet --wait --norestart"

# CMake (dependency dlib build system)
winget install --id Kitware.CMake -e --accept-source-agreements --accept-package-agreements
```

Setelah keempat instalasi selesai, **tutup dan buka ulang PowerShell** agar variabel `PATH` (Python, Git, CMake) ter-refresh, lalu verifikasi:

```powershell
py -3.11 --version    # mis. Python 3.11.9
git --version         # mis. git version 2.46.x.windows.1
cmake --version       # mis. cmake version 3.30.x
```

> Bila salah satu perintah gagal di-resolve, pastikan Anda memakai Windows yang sudah ter-update (`winget` butuh App Installer ≥ 1.17). Sebagai alternatif, instalasi Python 3.11 manual dapat diambil dari [python.org](https://www.python.org/downloads/release/python-3119/) (jangan lupa centang **Add Python to PATH** dan **Install for all users**).

#### 8.A.3 Catatan dependensi `dlib` di Windows

Paket `face-recognition` di `edge_server/requirements.txt` membutuhkan `dlib`, dan `dlib` **harus dikompilasi dari source** di Windows (PyPI tidak menyediakan wheel resmi untuk Python 3.11). Inilah alasan §8.A.2 memasang Visual C++ Build Tools + CMake. Bila `pip install dlib` tetap gagal di laptop Anda, pilih salah satu dari dua opsi alternatif berikut:

**Opsi (a) — Pakai wheel `dlib` pra-kompilasi pihak ketiga**

Jalankan di dalam virtualenv yang aktif (lihat §8.A.4):

```powershell
# Wheel dlib 19.22.99 untuk Python 3.11 / Windows x64 dari repo komunitas (sachadee/Dlib)
pip install https://github.com/sachadee/Dlib/raw/main/dlib-19.22.99-cp311-cp311-win_amd64.whl
pip install face-recognition --no-deps
```

Wheel pihak ketiga di-host di GitHub publik dan sering dipakai komunitas `face_recognition`; tetap audit URL dan checksum sebelum pakai di lingkungan produksi. Setelah baris ini, `pip install -r edge_server\requirements.txt` akan melewati build `dlib` karena sudah ter-install.

**Opsi (b) — Ganti engine ke `mediapipe` (lebih ringan, tanpa dlib)**

Bila Anda tidak ingin mem-build `dlib` sama sekali, ganti engine recognition di edge ke `mediapipe` Google. Mediapipe menyediakan wheel resmi untuk Windows + Python 3.11 dan jauh lebih ringan (≈ 60 MB vs ≈ 500 MB toolchain VC).

```powershell
pip install mediapipe==0.10.14 opencv-python==4.10.0.84 numpy"<2"
```

Lalu sesuaikan `edge_server\recognizer.py` agar memakai `mediapipe.solutions.face_detection` untuk deteksi wajah dan `mediapipe.solutions.face_mesh` (468 landmark) sebagai sumber embedding. Garis besar perubahan:

1. Ganti blok `import face_recognition` menjadi `import mediapipe as mp` dan import `cv2` + `numpy`.
2. Inisialisasi detektor di konstruktor `FaceRecognizer.__init__`:
   ```python
   self._mp_detection = mp.solutions.face_detection.FaceDetection(model_selection=0, min_detection_confidence=0.5)
   self._mp_mesh = mp.solutions.face_mesh.FaceMesh(static_image_mode=True, max_num_faces=1, refine_landmarks=False)
   ```
3. Implementasikan method `_encode(image_bgr)` yang mengembalikan vektor embedding panjang 936 (468 landmark × 2 koordinat normalisasi) sebagai `numpy.ndarray` float32.
4. Ganti perhitungan jarak `face_recognition.face_distance` dengan `numpy.linalg.norm(a - b)` (Euclidean) dan kalibrasikan ulang `RECOGNITION_THRESHOLD` di `.env` ke nilai yang sesuai (mediapipe biasanya butuh threshold lebih besar, sekitar `0.40`–`0.55` setelah L2-normalisasi).
5. File cache `embeddings.npz` tetap kompatibel karena hanya menyimpan `numpy` array per `employee_id`.

Setelah modifikasi, jalankan `python -m edge_server.main` dan pastikan log `Synced N embeddings` tetap muncul. Opsi (b) cocok untuk laptop entry-level tanpa GPU dan tanpa ruang disk untuk Visual Studio Build Tools.

#### 8.A.4 Clone repo + virtualenv

```powershell
# Clone ke C:\sapa (path pendek, hindari spasi/Unicode)
cd C:\
git clone https://github.com/farhanwijayanto/SAPA-Dashboard.git C:\sapa
cd C:\sapa

# Buat virtualenv pakai launcher py -3.11 (memastikan versi yang benar walau Python lain ter-install)
py -3.11 -m venv venv

# Aktifkan virtualenv di PowerShell
.\venv\Scripts\Activate.ps1
```

Bila Anda lebih nyaman dengan **Command Prompt (CMD)** klasik:

```cmd
cd C:\sapa
venv\Scripts\activate.bat
```

Setelah prompt berubah menjadi `(venv) PS C:\sapa>`, install dependensi edge:

```powershell
python -m pip install --upgrade pip wheel setuptools
pip install -r edge_server\requirements.txt
```

> Build pertama bisa memakan 5–15 menit karena `dlib` di-compile dari source. Bila terlalu lama atau gagal, lompat ke Opsi (a)/(b) di §8.A.3 lalu ulangi `pip install -r edge_server\requirements.txt`.

#### 8.A.5 Konfigurasi `.env` Windows

Salin `edge_server\.env.example` menjadi `.env`:

```powershell
Copy-Item edge_server\.env.example edge_server\.env
notepad edge_server\.env
```

Isi minimal nilai berikut (dapatkan token + kunci dari §8.3 di jalur Linux yang sudah ada — perintah `curl` dan `kubectl` dapat dijalankan langsung di PowerShell karena Windows 10 21H2+ sudah memuat `curl.exe` bawaan):

| Nama env          | Contoh nilai                                                             | Sumber                                        |
|-------------------|--------------------------------------------------------------------------|-----------------------------------------------|
| `SAPA_API_BASE`   | `https://sapa.farhn.dev/api`                                             | URL backend SAPA produksi                     |
| `SAPA_API_TOKEN`  | `<JWT manager>`                                                          | output `POST /api/login` user `manager`       |
| `EDGE_INGEST_KEY` | `<EDGE_INGEST_KEY>`                                                      | `kubectl -n sapa get secret sapa-backend-secret -o jsonpath='{.data.EDGE_INGEST_KEY}'` lalu `base64 -d` |
| `MQTT_BROKER`     | `sapa.farhn.dev`                                                         | host broker (NodePort di VPS)                 |
| `MQTT_PORT`       | `31883`                                                                  | NodePort Mosquitto                            |
| `MQTT_USERNAME`   | `edge`                                                                   | user MQTT yang dibuat di §6.2                 |
| `MQTT_PASSWORD`   | `<password user edge>`                                                   | password plaintext dari §6.2                  |
| `CAMERA_INDEX`    | `0`                                                                      | indeks webcam (0 = default; 1/2 jika multi)   |

Field `RECOGNITION_THRESHOLD`, `COOLDOWN_SECONDS`, `SYNC_EVERY_SECONDS`, `FRAME_PUSH_FPS` boleh dibiarkan dengan default di `.env.example`.

#### 8.A.6 Menjalankan service edge — dua mode

**Mode (a) — foreground (untuk testing pertama kali)**

```powershell
cd C:\sapa
.\venv\Scripts\Activate.ps1
python -m edge_server.main
```

Anda akan melihat log realtime: `MQTT connected rc=0`, `Synced N employee faces -> N embeddings`, dan `Edge server running. threshold=0.55 cooldown=5s`. Tekan `Ctrl+C` untuk berhenti.

**Mode (b) — Windows service permanen via NSSM**

NSSM (Non-Sucking Service Manager) adalah cara termudah membungkus skrip Python sebagai Windows service yang otomatis restart saat crash dan bisa start saat boot. Unduh dari [https://nssm.cc/download](https://nssm.cc/download), ekstrak, dan letakkan `nssm.exe` di `C:\Tools\nssm\nssm.exe` (atau folder yang ada di `PATH`).

Dari **PowerShell sebagai Administrator**:

```powershell
# Daftarkan service
nssm install sapa-edge

# Atau langsung set semua field via CLI (tanpa GUI):
nssm install sapa-edge "C:\sapa\venv\Scripts\python.exe"
nssm set sapa-edge AppParameters "-m edge_server.main"
nssm set sapa-edge AppDirectory "C:\sapa"
nssm set sapa-edge AppEnvironmentExtra "PYTHONPATH=C:\sapa" "PYTHONUNBUFFERED=1"
nssm set sapa-edge AppStdout "C:\sapa\logs\sapa-edge.out.log"
nssm set sapa-edge AppStderr "C:\sapa\logs\sapa-edge.err.log"
nssm set sapa-edge Start SERVICE_AUTO_START
nssm set sapa-edge AppExit Default Restart
nssm set sapa-edge AppRestartDelay 3000

# Jalankan service
New-Item -ItemType Directory -Force -Path C:\sapa\logs | Out-Null
nssm start sapa-edge
nssm status sapa-edge   # harus "SERVICE_RUNNING"
```

Field penting:
- **Application** = path absolut ke `python.exe` di dalam virtualenv (`C:\sapa\venv\Scripts\python.exe`).
- **Startup directory** = `C:\sapa` agar `python -m edge_server.main` menemukan modul.
- **AppEnvironmentExtra** memastikan `PYTHONPATH` benar dan output tidak ter-buffer (sehingga log NSSM aktual realtime). NSSM **tidak otomatis** memuat `.env` — service membaca `.env` lewat `python-dotenv` yang sudah ada di `edge_server/config.py` selama working directory benar.

**Mode (b alternatif) — Windows Task Scheduler**

Bila admin tidak boleh memasang NSSM, gunakan Task Scheduler bawaan Windows:

```powershell
$action = New-ScheduledTaskAction `
  -Execute "C:\sapa\venv\Scripts\python.exe" `
  -Argument "-m edge_server.main" `
  -WorkingDirectory "C:\sapa"

$trigger = New-ScheduledTaskTrigger -AtLogOn

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "SAPA Edge AI" -Action $action -Trigger $trigger -Principal $principal -Settings $settings
Start-ScheduledTask -TaskName "SAPA Edge AI"
```

Trigger `At log on` cocok untuk laptop yang otomatis login ke akun gate; bila laptop berjalan tanpa login interaktif, pakai trigger `At startup` dengan akun service yang punya hak `Log on as a service` (Local Security Policy → User Rights Assignment).

#### 8.A.7 Aturan Windows Defender Firewall

Egress ke broker MQTT (TCP 31883) dan HTTPS (TCP 443) biasanya sudah diizinkan oleh profil firewall default, tetapi bila Anda mengaktifkan profil `Public` yang ketat, daftarkan rule eksplisit dari **PowerShell sebagai Administrator**:

```powershell
New-NetFirewallRule -DisplayName "SAPA MQTT egress" `
  -Direction Outbound -Protocol TCP -RemotePort 31883 -Action Allow

New-NetFirewallRule -DisplayName "SAPA HTTPS egress" `
  -Direction Outbound -Protocol TCP -RemotePort 443 -Action Allow
```

Tidak perlu rule **Inbound** — laptop edge hanya melakukan koneksi keluar ke VPS, tidak menerima koneksi masuk dari internet.

#### 8.A.8 Verifikasi pasca-instalasi

1. **Cek log service.** Bila pakai NSSM, tail file log:
   ```powershell
   Get-Content -Tail 50 -Wait C:\sapa\logs\sapa-edge.out.log
   ```
   Bila pakai Task Scheduler, log Python di-redirect lewat `Action`-nya; alternatif gunakan Event Viewer:
   ```powershell
   Get-EventLog -LogName Application -Source "SAPA Edge AI" -Newest 20
   ```
2. **Konfirmasi koneksi.** Di log harus muncul minimal dua baris kunci:
   - `MQTT connected rc=0` — broker sapa.farhn.dev:31883 menerima auth user `edge`.
   - `Synced N employee faces -> N embeddings` — `GET /api/edge/faces` + download `/api/static/faces/*.jpg` sukses.
3. **Buka halaman browser edge.** Buka **Microsoft Edge** atau **Chrome** di laptop Windows yang sama:
   ```
   https://sapa.farhn.dev/edge
   ```
   Browser akan minta izin kamera (HTTPS wajib agar `getUserMedia` aktif). Setelah diizinkan, frame kamera mulai dikirim ke `POST /api/edge/frame` setiap ~350 ms.
4. **Cek dashboard manager.** Buka `https://sapa.farhn.dev/` di PC lain (bukan di laptop edge), login sebagai manager, masuk tab **System** → badge **Gate ONLINE** harus menyala dan tombol **Open/Close** enabled. Tab **System → Live Camera** menampilkan frame realtime dari laptop.

#### 8.A.8.5 Verifikasi rantai end-to-end VPS ↔ Edge ↔ ESP32

Bagian ini adalah **acceptance test** untuk memastikan ketiga jalur di topologi §8.A.0 (HTTPS, MQTT laptop, MQTT ESP32) sudah saling tersambung. Lakukan urutan ini setelah service `sapa-edge` jalan dan ESP32 sudah di-flash (§9). Setiap langkah memvalidasi satu jalur. Bila salah satu gagal, lompat ke §8.A.9 sebelum lanjut.

**Langkah 1 — Jalur 1: Edge → Backend (HTTPS).** Dari **PowerShell di laptop Windows**, panggil tiga endpoint AI Gate seperti dilakukan service di balik layar. Semua harus mengembalikan status 2xx:

```powershell
# Token + key dari Secret VPS (lihat §10.A.1 untuk cara generate)
$env:TOKEN     = "<JWT manager dari /api/login>"
$env:EDGE_KEY  = "<EDGE_INGEST_KEY dari kubectl get secret>"

# (a) listing faces — auth pakai X-EDGE-KEY
Invoke-WebRequest -UseBasicParsing -Uri "https://sapa.farhn.dev/api/edge/faces" `
  -Headers @{ "X-EDGE-KEY" = $env:EDGE_KEY } | Select-Object StatusCode, Content

# (b) face-match — auth pakai X-EDGE-KEY, ini yang akan men-trigger publish ke sapa/gate
Invoke-WebRequest -UseBasicParsing -Uri "https://sapa.farhn.dev/api/edge/face-match" `
  -Method POST `
  -Headers @{ "X-EDGE-KEY" = $env:EDGE_KEY; "Content-Type" = "application/json" } `
  -Body '{"is_valid":true,"employee_id":"513061","confidence":0.91}' `
  | Select-Object StatusCode, Content
```

Sukses bila `StatusCode = 200` dan body face-match berisi `{"action":"open","employee_id":"513061","logged":true}`. Bila 401 → `EDGE_INGEST_KEY` salah; 503 → backend tidak bisa publish ke MQTT (cek §10.A.5).

**Langkah 2 — Jalur 2: Edge laptop ↔ VPS Mosquitto.** Subscribe topic system MQTT dari laptop Windows untuk membuktikan jalur MQTT TCP 31883 hidup dan kredensial user `edge` benar. Pakai `mosquitto_sub` versi Windows yang ikut terbawa saat install **Mosquitto for Windows** ([https://mosquitto.org/download/](https://mosquitto.org/download/), pilih installer minimal "Service" tidak perlu di-enable — kita hanya butuh CLI tools-nya):

```powershell
# Verifikasi mosquitto_sub.exe ada di PATH
mosquitto_sub --help | Select-Object -First 1   # harus print "mosquitto_sub version ..."

# Subscribe ke heartbeat selama 10 detik. -W 10 = berhenti otomatis sesudah 10 detik.
mosquitto_sub -h sapa.farhn.dev -p 31883 `
  -u edge -P "<password user edge dari .env>" `
  -t 'sapa/device/heartbeat' -t 'sapa/gate' `
  -v -W 10
```

Output minimal yang harus muncul dalam 10 detik:

```
sapa/device/heartbeat {"online": true, "source": "edge", "ts": <unix>}
```

Yang artinya: laptop **bisa konek** ke broker (kredensial OK + NodePort 31883 dapat diakses), dan **service `sapa-edge` di laptop sedang publish heartbeat tiap 5 detik** (lihat `_heartbeat_loop` di `edge_server/main.py`). Bila sub langsung gagal dengan `Connection Refused: not authorised` → password user `edge` salah; `Connection refused` saja → firewall lokal/korporat blokir port 31883 (lihat §8.A.7).

**Langkah 3 — Jalur 3: ESP32 ↔ VPS Mosquitto + flow gate command.** Sambil `mosquitto_sub` masih jalan, picu gate command dari shell lain. Backend akan publish `sapa/gate {"action":"open",...}` → Mosquitto kirim ke **dua subscriber**: laptop edge (kalau di-subscribe) dan ESP32. Caranya:

```powershell
# Terminal A (biarkan jalan): subscribe ke sapa/gate
mosquitto_sub -h sapa.farhn.dev -p 31883 `
  -u edge -P "<password edge>" `
  -t 'sapa/gate' -v
```

```powershell
# Terminal B: trigger face-match valid via dashboard ATAU pakai curl
Invoke-WebRequest -UseBasicParsing -Uri "https://sapa.farhn.dev/api/edge/face-match" `
  -Method POST `
  -Headers @{ "X-EDGE-KEY" = $env:EDGE_KEY; "Content-Type" = "application/json" } `
  -Body '{"is_valid":true,"employee_id":"513061"}'
```

Tiga hal yang harus terjadi **bersamaan** dalam <1 detik:

1. Terminal A menampilkan: `sapa/gate {"action": "open", "employee_id": "513061", "ts": "2026-..."}`
2. **ESP32 servo membuka** (terdengar bunyi servo berputar; LED status berubah).
3. **Buzzer ESP32 beep 1×** sebagai konfirmasi gate dibuka.

Bila Terminal A menerima pesan tetapi ESP32 tidak bergerak → masalahnya di sisi ESP32 (cek WiFi, kredensial user `esp32`, atau topic subscribe di `sapa_gate.ino`); periksa Serial Monitor Arduino IDE saat trigger dijalankan.

**Langkah 4 — Verifikasi catatan absensi ke MongoDB.** `POST /api/edge/face-match` di Langkah 3 juga menulis ke MongoDB collection `attendance_logs` (Requirement 5.1). Cek dari laptop Windows lewat dashboard:

```
https://sapa.farhn.dev/  (login manager)
  → tab Attendance → filter date = hari ini
  → seharusnya muncul baris baru employee_id 513061, status "present", source "ai_gate"
```

Atau cek langsung lewat `kubectl` di VPS:

```bash
kubectl -n sapa exec deploy/sapa-mongo -- mongosh sapa --quiet --eval \
  'db.attendance_logs.find({employee_id:"513061", source:"ai_gate"}).sort({timestamp:-1}).limit(1).toArray()'
```

Bila Langkah 1–4 semua hijau, rantai **Edge Windows ↔ VPS ↔ ESP32** terverifikasi dan production-ready.

#### 8.A.9 Troubleshooting Windows

Empat penyebab kegagalan paling umum saat menjalankan edge service di Windows native, beserta langkah resolusinya:

**(a) `pip install dlib` gagal — Visual C++ Build Tools belum terpasang**

Gejala: build terhenti dengan pesan `error: Microsoft Visual C++ 14.0 or greater is required` atau `CMake Error: Could not find any instance of Visual Studio`.

Resolusi: ulang §8.A.2 perintah `winget install Microsoft.VisualStudio.2022.BuildTools ... --override ...` sebagai **Administrator**, lalu **restart laptop** (penting agar `vswhere.exe` registry ter-refresh). Verifikasi instalasi dengan:
```powershell
& "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -products * -requires Microsoft.VisualStudio.Workload.VCTools -property installationPath
```
Jika tetap gagal, gunakan Opsi (a) wheel pra-kompilasi atau Opsi (b) mediapipe di §8.A.3.

**(b) Kamera tidak terdeteksi — izin Privacy Camera mati**

Gejala: log berisi `cv2.VideoCapture(0) failed` atau frame yang tertangkap berwarna hitam pekat. `cv2.VideoCapture(0).isOpened()` mengembalikan `False`.

Resolusi: buka **Settings → Privacy & security → Camera** lalu pastikan:
- **Camera access** (top-level) = `On`
- **Let apps access your camera** = `On`
- **Let desktop apps access your camera** = `On` (toggle paling bawah; sering terlewat)

Restart service `sapa-edge` setelah toggle diubah:
```powershell
nssm restart sapa-edge
```

**(c) Service NSSM `Paused` karena `.env` tidak terbaca**

Gejala: `nssm status sapa-edge` menampilkan `SERVICE_PAUSED` dan log berisi `KeyError: 'SAPA_API_TOKEN'` atau Pydantic validation error karena env kosong.

Penyebab: `python-dotenv` membaca `.env` dari **current working directory**, bukan dari lokasi script. Bila NSSM `AppDirectory` salah (mis. tertinggal `C:\Windows\System32`), `.env` tidak ditemukan.

Resolusi:
```powershell
nssm set sapa-edge AppDirectory "C:\sapa"
nssm set sapa-edge AppEnvironmentExtra "PYTHONPATH=C:\sapa" "PYTHONUNBUFFERED=1"
nssm restart sapa-edge
```
Verifikasi: `nssm get sapa-edge AppDirectory` harus mengembalikan `C:\sapa`. Bila masih gagal, set env vars langsung lewat NSSM (`AppEnvironmentExtra`) sebagai workaround sementara untuk minimal `SAPA_API_BASE`, `SAPA_API_TOKEN`, `EDGE_INGEST_KEY`, `MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `CAMERA_INDEX`.

**(d) PowerShell menolak menjalankan `Activate.ps1` — ExecutionPolicy memblokir**

Gejala: saat `\.\venv\Scripts\Activate.ps1`, PowerShell mengeluarkan `cannot be loaded because running scripts is disabled on this system`.

Resolusi: jalankan sekali per akun pengguna (tidak butuh Administrator):
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```
Lalu tutup-buka PowerShell dan ulangi `\.\venv\Scripts\Activate.ps1`. Sebagai alternatif sementara, pakai CMD klasik dengan `venv\Scripts\activate.bat` yang tidak terpengaruh ExecutionPolicy.

#### 8.A.10 Update kode di laptop Windows

Padanan langkah update Linux di §12 untuk laptop Windows yang memakai NSSM:

```powershell
cd C:\sapa
git pull
.\venv\Scripts\Activate.ps1
pip install -r edge_server\requirements.txt   # bila ada dependency baru
nssm restart sapa-edge
```

Bila pakai Task Scheduler, ganti baris terakhir dengan:

```powershell
Stop-ScheduledTask  -TaskName "SAPA Edge AI"
Start-ScheduledTask -TaskName "SAPA Edge AI"
```

Tail log lagi (`Get-Content -Tail 50 -Wait C:\sapa\logs\sapa-edge.out.log`) untuk memastikan service kembali `MQTT connected rc=0` setelah pull.

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

## 10.A) Verifikasi AI Gate

Modul `backend/ai_gate.py` mengekspos tiga endpoint baru di prefix `/api/edge` yang dipakai Dashboard (upload foto wajah), Edge laptop (tarik daftar wajah, kirim hasil face match), dan ESP32 (lewat MQTT topic `sapa/gate`). Sub-bab ini berisi langkah verifikasi yang harus dijalankan setelah §10 selesai, sebelum gate dianggap siap produksi.

> Semua perintah `curl` di bawah harus mengembalikan **HTTP status code dalam rentang 2xx** sebagai tanda sukses. `curl -i` ditampilkan agar header response (termasuk status line `HTTP/1.1 200 OK` atau `HTTP/2 200`) ikut tercetak.

### 10.A.1 Verifikasi endpoint REST

Siapkan dulu dua variabel lingkungan di shell yang akan dipakai:

```bash
# JWT manager — sama seperti §8.3.1
export TOKEN=$(curl -s -X POST https://sapa.farhn.dev/api/login \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode username=manager \
  --data-urlencode password=manager \
  | jq -r .access_token)

# EDGE_INGEST_KEY — baca dari Secret backend
export EDGE_KEY=$(kubectl -n sapa get secret sapa-backend-secret \
  -o jsonpath='{.data.EDGE_INGEST_KEY}' | base64 -d)
```

**(a) `POST /api/edge/upload-face/{employee_id}`** — Manager mengunggah foto wajah pegawai (`employee_id=513061`). Foto contoh `513061.jpg` sudah ada di repo (`backend/uploads/faces/513061.jpg`); Anda dapat memakai file lokal dengan ekstensi `.jpg` atau `.png` ≤ 5 MB.

```bash
curl -i -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@./513061.jpg" \
  https://sapa.farhn.dev/api/edge/upload-face/513061
# Sukses bila status line menampilkan HTTP 2xx (200 OK) dan body
# {"employee_id":"513061","saved_path":"uploads/faces/513061.jpg"}
```

**(b) `GET /api/edge/faces`** — Edge laptop menarik daftar foto referensi. Endpoint ini menerima `X-EDGE-KEY` (mesin-ke-mesin) atau JWT manager/admin.

```bash
curl -i -H "X-EDGE-KEY: $EDGE_KEY" \
  https://sapa.farhn.dev/api/edge/faces
# Sukses bila status line menampilkan HTTP 2xx (200 OK) dan body JSON
# {"faces":[{"employee_id":"513061","url":"/api/static/faces/513061.jpg"}, ...]}
```

**(c) `POST /api/edge/face-match`** — Edge laptop melaporkan hasil pencocokan wajah; backend akan mem-publish ke topic MQTT `sapa/gate` dan menulis `attendance_logs`/`audit_logs` di MongoDB.

```bash
curl -i -X POST \
  -H "X-EDGE-KEY: $EDGE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"is_valid":true,"employee_id":"513061","confidence":0.91}' \
  https://sapa.farhn.dev/api/edge/face-match
# Sukses bila status line menampilkan HTTP 2xx (200 OK) dan body
# {"action":"open","employee_id":"513061","logged":true}
```

> Bila salah satu perintah mengembalikan status di luar 2xx (mis. 401, 415, 503), lompat ke §10.A.5 **Troubleshooting AI Gate** sebelum melanjutkan.

### 10.A.2 Verifikasi pesan MQTT di topic `sapa/gate`

Setelah `POST /api/edge/face-match` dipanggil, backend akan mem-publish JSON `{"action": "open"|"invalid", "employee_id": "..."}` ke topic `sapa/gate` di broker. Pakai `mosquitto_sub` dari host mana saja yang dapat menjangkau NodePort 31883:

```bash
# Pasang MQTT_PASSWORD = password user 'backend' yang Anda generate di §6.2
mosquitto_sub -h <broker> -p 31883 -u backend -P "$MQTT_PASSWORD" -t sapa/gate -v
# Contoh payload yang muncul setelah POST /api/edge/face-match:
#   sapa/gate {"action": "open", "employee_id": "513061", "ts": "2025-01-15T10:30:00.123Z"}
#   sapa/gate {"action": "invalid", "employee_id": "tamu-001", "ts": "2025-01-15T10:31:05.987Z"}
```

`<broker>` di `-h` boleh diisi `sapa.farhn.dev` (publik via NodePort) atau IP publik VPS. Selama backend bisa publish dan ESP32 bisa subscribe topic yang sama, jalur Edge → Backend → ESP32 sudah berfungsi.

### 10.A.3 PVC `Faces_Storage` dan persistensi foto wajah

Foto wajah disimpan di direktori `backend/uploads/faces/` di dalam pod backend, di-mount dari PersistentVolumeClaim `sapa-backend-uploads` dengan access mode `ReadWriteOnce` dan kapasitas minimum `1Gi` (manifes saat ini menyetel `5Gi` sebagai header room). Mount point dan PVC ini didefinisikan di `k8s/backend-deployment.yaml` pada bagian `volumeMounts` (`/app/backend/uploads`) serta resource `PersistentVolumeClaim` di file yang sama. PVC inilah yang menjamin foto pegawai tidak hilang saat pod backend di-restart, di-evict, atau di-rolling-update — tanpa PVC, setiap restart pod akan menghapus seluruh `Faces_Storage`.

```bash
# Cek PVC sudah Bound
kubectl -n sapa get pvc sapa-backend-uploads
# Cek mount di dalam pod
kubectl -n sapa exec deploy/sapa-backend -- ls -lah /app/backend/uploads/faces/
```

### 10.A.4 Secret `sapa-backend-secret` untuk MQTT publish

Agar modul AI Gate dapat mem-publish ke `sapa/gate`, Secret Kubernetes `sapa-backend-secret` di namespace `sapa` **wajib** memuat `MQTT_USERNAME=backend` dan `MQTT_PASSWORD=<password user backend di Mosquitto>` (lihat langkah §6.4). Secret ini harus sudah ada (applied) sebelum pod backend di-start, karena variabel tersebut dibaca pada saat startup modul `ai_gate.py`. Bila Secret belum diisi, modul akan gagal autentikasi ke broker dan setiap `POST /api/edge/face-match` berakhir dengan HTTP 503 `mqtt_unavailable`.

```bash
# Cek key MQTT_USERNAME / MQTT_PASSWORD pada Secret backend
kubectl -n sapa get secret sapa-backend-secret \
  -o jsonpath='{.data.MQTT_USERNAME}' | base64 -d; echo
kubectl -n sapa get secret sapa-backend-secret \
  -o jsonpath='{.data.MQTT_PASSWORD}' | base64 -d; echo
# Bila salah satu kosong, jalankan §6.4 untuk patch + rollout restart deploy/sapa-backend.
```

### 10.A.5 Troubleshooting AI Gate

Tiga gejala paling umum saat verifikasi AI Gate gagal, lengkap dengan langkah resolusi dan satu perintah verifikasi cepat:

#### Pesan tidak muncul di topic `sapa/gate`

Penyebab yang biasa: (1) pod backend belum konek ke Mosquitto karena `MQTT_USERNAME`/`MQTT_PASSWORD` di Secret kosong atau salah, (2) ACL Mosquitto belum mengizinkan user `backend` mem-publish ke `sapa/gate`, (3) env `MQTT_BROKER`/`MQTT_PORT` di ConfigMap `sapa-backend-config` salah arah (mis. menunjuk ke host yang tidak ada).

Resolusi:

1. Cek log modul `ai_gate` apakah ada baris `MQTT connected rc=0` atau pesan kegagalan publish:
   ```bash
   kubectl -n sapa logs deploy/sapa-backend | grep -E 'ai_gate|mqtt|sapa/gate'
   ```
2. Verifikasi broker hidup dan ACL benar dengan men-subscribe topic system dari host yang sama:
   ```bash
   mosquitto_sub -h sapa.farhn.dev -p 31883 -u backend -P "$MQTT_PASSWORD" -t '$SYS/#' -C 5
   ```
   Bila `Connection Refused: not authorised` → password salah; bila `Connection refused` saja → broker/NodePort tidak terjangkau.
3. Setelah Secret diperbaiki, lakukan `kubectl -n sapa rollout restart deploy/sapa-backend` lalu ulangi §10.A.1 (c) dan §10.A.2.

#### Endpoint `/api/edge/*` mengembalikan 4xx/5xx

Penyebab yang biasa: (1) `EDGE_INGEST_KEY` di Secret tidak sama dengan yang dipakai Edge laptop sehingga 401, (2) JWT manager kedaluwarsa sehingga `POST /api/edge/upload-face/...` 401, (3) MongoDB tidak terjangkau sehingga 500 `logging_unavailable` atau 503 `mqtt_unavailable`, (4) request body face match tidak sesuai schema sehingga 422.

Resolusi:

1. Cek health endpoint backend dari dalam pod (memutus dugaan masalah Ingress/TLS):
   ```bash
   kubectl -n sapa exec deploy/sapa-backend -- curl -sS http://127.0.0.1:8000/health
   ```
   Bila `200 OK` tapi endpoint `/api/edge/*` tetap 4xx, masalah ada di header auth atau body request.
2. Bandingkan `EDGE_INGEST_KEY` di Secret dengan yang dipakai client:
   ```bash
   kubectl -n sapa get secret sapa-backend-secret \
     -o jsonpath='{.data.EDGE_INGEST_KEY}' | base64 -d; echo
   ```
   Bila berbeda dengan `.env` Edge laptop, sinkronkan kedua sisi lalu restart `sapa-edge.service`.
3. Cek log backend untuk melihat pesan validasi yang dilempar modul:
   ```bash
   kubectl -n sapa logs --tail=200 deploy/sapa-backend | grep -E 'edge|ai_gate|HTTP/'
   ```

#### PVC `Faces_Storage` tidak ter-mount

Penyebab yang biasa: (1) PVC `sapa-backend-uploads` masih `Pending` karena tidak ada StorageClass default di node, (2) volume sudah di-mount tapi `/app/backend/uploads/faces/` kosong sehingga `GET /api/edge/faces` selalu mengembalikan `{"faces":[]}`, (3) permission direktori salah sehingga modul gagal `os.replace` saat upload.

Resolusi:

1. Cek status dan event PVC untuk melihat alasan kegagalan binding:
   ```bash
   kubectl describe pvc sapa-backend-uploads -n sapa
   ```
   Bila `Status: Pending` dengan event `no persistent volumes available`, pasang StorageClass default (mis. `local-path-provisioner`) lalu re-apply manifes.
2. Verifikasi bahwa direktori benar-benar ter-mount di pod dan dapat ditulis oleh user FastAPI:
   ```bash
   kubectl -n sapa exec deploy/sapa-backend -- ls -lah /app/backend/uploads/faces/
   ```
   Output harus menampilkan direktori (mode `drwxr-xr-x`) dan setiap file `.jpg` yang sudah diunggah. Bila perintah `ls` melaporkan `No such file or directory`, mount gagal — periksa kembali bagian `volumeMounts` di `k8s/backend-deployment.yaml` dan re-apply manifes.

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
| `kubeadm init` error `[ERROR Mem]` | Pakai `--config k8s/kubeadm-config.yaml --ignore-preflight-errors=Mem,Swap` (lihat §4.3 Opsi B), pastikan swap 4 GB aktif (lihat §2.4). |
| Reset kubeadm tanpa memutus SSH | **JANGAN** jalankan `iptables -F`/`iptables -t nat -F` saat UFW aktif. Pakai langkah aman di §11.1. |
| Pod `Pending` `Insufficient memory` | Node sedang sempit; kurangi replica yang tidak perlu (`mqtt-deployment.yaml` versi dev). Atau naikkan swap, lalu `kubectl rollout restart deploy/...`. |
| SSH putus / VPS reboot saat deploy | Hampir selalu karena kernel OOM-kill `sshd`. Cek `dmesg -T \| grep -i oom`. Solusi: aktifkan swap (§2.4) + pakai `kubeadm-config.yaml` (`systemReserved`+`evictionSoft`) supaya kubelet evict pod jauh sebelum sshd kena. |

### 11.1 Reset kubeadm tanpa memutus SSH

**JANGAN** jalankan `iptables -F` atau `iptables -t nat -F` di VPS yang UFW-nya aktif. Perintah itu menghapus rule UFW yang membolehkan SSH masuk + rule conntrack `RELATED,ESTABLISHED` — paket SSH balik akan langsung di-DROP, koneksi Anda freeze sampai timeout. Pakai urutan ini:

```bash
# 1. reset kubeadm (sudah ikut bersihkan rule milik kube-proxy/kubelet)
sudo kubeadm reset -f

# 2. bersihkan CNI
sudo rm -rf /etc/cni/net.d /var/lib/cni /opt/cni/bin/flannel*
sudo ip link delete cni0      2>/dev/null
sudo ip link delete flannel.1 2>/dev/null

# 3. hapus HANYA chain milik kubernetes/docker, bukan UFW
sudo iptables-save | grep -v -E '^:KUBE|^-A KUBE|^:DOCKER|^-A DOCKER|^:CNI|^-A CNI|^:FLANNEL|^-A FLANNEL' | sudo iptables-restore
for t in filter nat mangle; do
  sudo iptables -t $t -S | awk '/^-N (KUBE|DOCKER|CNI|FLANNEL)/ {print $2}' \
    | xargs -r -I{} sudo iptables -t $t -X {}
done

# 4. reload UFW agar rule SSH+80+443+31883 kembali aktif
sudo ufw reload

# 5. restart container runtime
sudo systemctl restart containerd
```

Kalau terlanjur SSH putus akibat `iptables -F`: masuk lewat **console/VNC** dari panel provider VPS, lalu jalankan `sudo ufw disable` (mengembalikan policy default `ACCEPT`) — SSH segera hidup. Setelah login lagi, jalankan `sudo ufw enable` ulang dan reset bersih dengan langkah di atas.

---

## 13) Tuning untuk VPS dengan RAM kecil / dinamis (burstable)

VPS "dynamic RAM" biasanya menampilkan baseline ~1 GB padahal plan-nya bisa burst ke beberapa GB saat ada beban. kubeadm preflight melihat angka *current* di `/proc/meminfo` saja, jadi sering ditolak walau plan-nya cukup. Kombinasi pengaturan berikut sudah dipakai dokumen ini dan **tidak** akan menyebabkan VPS reboot atau koneksi putus.

| Pengaturan | Lokasi | Tujuan |
|------------|--------|--------|
| Swap file 4 GB + `vm.swappiness=10` | host (§2.4) | Cushion ketika RAM live di bawah limit. Kernel pakai disk dulu, **bukan OOM-kill**. |
| `kubeadm init --ignore-preflight-errors=Mem,Swap` | §4.3 Opsi B | Lewatkan check awal yang gagal di RAM live <1700 MB. |
| `failSwapOn: false`, `memorySwap.swapBehavior: LimitedSwap` | `k8s/kubeadm-config.yaml` | kubelet tidak crash saat swap aktif (didukung resmi sejak 1.28). |
| `systemReserved` 256 Mi + `kubeReserved` 256 Mi | `k8s/kubeadm-config.yaml` | Total 512 Mi disisihkan untuk OS+kubelet → `sshd`, containerd, kubelet aman walau pod menyala-padam. |
| `evictionSoft memory.available: 300Mi` (grace 30s) + `evictionHard 150Mi` | `k8s/kubeadm-config.yaml` | Kubelet **menggusur pod aplikasi** dulu sebelum kernel sampai pada titik OOM-kill. SSH/kubelet tetap hidup. |
| `requests` rendah (50m / 128 Mi), `limits` tinggi (1 CPU / 768 Mi) | `k8s/*-deployment.yaml` | Pod lulus scheduling saat RAM live kecil; saat VPS burst, pod boleh pakai sampai limit. |

Cara menerapkan kalau Anda sudah init dengan opsi default (RAM live cukup) tapi belakangan mau switch ke profil low-RAM:

```bash
# 13.1 aktifkan swap (jika belum)
if ! sudo swapon --show | grep -q swapfile; then
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

# 13.2 tambah konfigurasi kubelet runtime (drop-in)
sudo tee /etc/systemd/system/kubelet.service.d/20-low-ram.conf >/dev/null <<'EOF'
[Service]
Environment="KUBELET_EXTRA_ARGS=--fail-swap-on=false \
  --system-reserved=cpu=100m,memory=256Mi \
  --kube-reserved=cpu=100m,memory=256Mi \
  --eviction-hard=memory.available<150Mi,nodefs.available<5% \
  --eviction-soft=memory.available<300Mi \
  --eviction-soft-grace-period=memory.available=30s \
  --eviction-max-pod-grace-period=60"
EOF
sudo systemctl daemon-reload
sudo systemctl restart kubelet

# 13.3 verifikasi node aman
kubectl describe node | grep -A2 Allocatable
kubectl describe node | grep -i evict
```

Apa yang **TIDAK** disarankan (penyebab umum VPS reboot / SSH putus saat tertekan RAM):

- Mematikan swap di VPS dynamic-RAM. Hilangkan cushion → kernel langsung OOM-kill, sering kena `sshd` atau `kubelet`.
- Set `requests.memory` besar (≥512 Mi) untuk semua deployment di node single-control-plane RAM kecil. Pod akan `Pending`.
- `vm.overcommit_memory=2` di host. Kernel jadi terlalu konservatif, container Python (FastAPI/uvicorn) sering gagal `mmap`.
- Menjalankan `docker build` berat (face_recognition / dlib) di VPS yang sama. Build image edge-server **selalu** dilakukan di laptop, bukan VPS — VPS hanya butuh image backend (FastAPI ringan) dan frontend (Nginx + static).

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
