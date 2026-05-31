# SAPA Edge Server (Laptop)

Service Python yang berjalan di laptop dekat gate. Menghubungkan kamera +
face recognition lokal ke dashboard `sapa.farhn.dev`.

## PENTING: Dua "kamera" yang berbeda di dashboard

Ini sumber kebingungan paling umum. Ada DUA tempat kamera di SAPA:

| Lokasi | Sumber kamera | Cara kerja |
|--------|---------------|------------|
| **`sapa.farhn.dev/edge`** | Webcam **browser** yang membuka halaman itu | Halaman pakai `getUserMedia()` browser sendiri, lalu kirim frame ke `/edge/frame`. Ini untuk operator yang buka `/edge` langsung di laptop. |
| **Dashboard → System tab → Live Camera** | Frame yang di-push **edge server Python** | Membaca `/edge/frame.jpg` yang di-isi oleh `edge_server/main.py` lewat `push_frame()`. |

**Jadi:** kalau Anda jalankan `python -m edge_server.main`, kameranya muncul di
**Dashboard → System → Live Camera**, BUKAN di halaman `/edge`. Halaman `/edge`
adalah viewer browser terpisah.

Keduanya menulis ke endpoint `/edge/frame` yang sama, jadi yang terakhir push
yang tampil. Untuk produksi: jalankan edge server Python, lihat di System tab.

## Struktur folder

```
edge_server/
├── __init__.py          # marker package
├── .env.example         # template config -> copy ke .env
├── README.md            # file ini
├── requirements.txt     # dependencies
├── config.py            # load .env (default ke produksi sapa.farhn.dev)
├── recognizer.py        # face_recognition wrapper + cache embeddings.npz
├── sapa_client.py       # HTTP client ke VPS (robust: AI Gate + fallback legacy)
├── overlay.py           # gambar kotak hijau/merah
└── main.py              # entry point

# auto-generated saat run:
├── embeddings.npz       # cache 128-d embedding per karyawan
└── .env                 # config aktual (JANGAN commit)
```

## Quick start

### 1. Install dependencies

```powershell
cd C:\sapa
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r edge_server\requirements.txt
```

### 2. Konfigurasi .env

```powershell
Copy-Item edge_server\.env.example edge_server\.env
notepad edge_server\.env
```

Isi yang WAJIB:

```env
SAPA_API_BASE=https://sapa.farhn.dev/api    # WAJIB pakai /api
SAPA_USERNAME=manager
SAPA_PASSWORD=<password manager Anda>
EDGE_INGEST_KEY=<dari kubectl get secret sapa-backend-secret>
MQTT_BROKER=sapa.farhn.dev
MQTT_PORT=31883
MQTT_USERNAME=edge
MQTT_PASSWORD=<password user edge di Mosquitto>
CAMERA_INDEX=0
```

### 3. Jalankan

```powershell
python -m edge_server.main
```

Log yang menandakan SUKSES nyambung ke dashboard:

```
SAPA_API_BASE = https://sapa.farhn.dev/api
MQTT connected rc=0
Synced 3 faces -> 3 embeddings
Frame push OK -> dashboard Live Camera aktif    <-- INI tanda kamera nyambung
```

Lalu buka **Dashboard → System tab → Live Camera** untuk lihat feed kamera.

## Kenapa tidak nyambung? (troubleshoot)

| Gejala | Penyebab | Fix |
|--------|----------|-----|
| `Frame push gagal (HTTP 401)` | `EDGE_INGEST_KEY` salah/kosong | Cek `kubectl -n sapa get secret sapa-backend-secret -o jsonpath='{.data.EDGE_INGEST_KEY}' \| base64 -d` |
| `Frame push gagal (HTTP 404)` | `SAPA_API_BASE` salah (tanpa `/api`) | Set `SAPA_API_BASE=https://sapa.farhn.dev/api` |
| `Frame push gagal (HTTP 413)` | Frame > 1 MB | Turunkan resolusi (sudah di-set 640x480 + quality 70) |
| `Synced 0 faces` | Belum ada foto / auto-login gagal | Isi `SAPA_PASSWORD`. Upload foto via dashboard Add Employee |
| Live Camera kosong di System tab | Edge server belum push, atau frame stale >5s | Cek log `Frame push OK`. Frame lama dari browser `/edge` bisa menimpa |
| `MQTT connect failed` | Port 31883 blocked / password salah | Cek firewall + `MQTT_PASSWORD` user `edge` |
| Camera index error | Webcam dipakai app lain / index salah | Tutup app lain, coba `CAMERA_INDEX=1` |

## Cara kerja kotak hijau/merah (window lokal)

Window OpenCV "SAPA Edge" muncul di laptop (kalau ada display):
- **Kotak HIJAU** = wajah cocok dengan cache, label `513061 - John Doe (91%)`
- **Kotak MERAH** = wajah tidak dikenali, label `Unknown`
- Banner atas: `SAPA Edge | faces=N | MQTT=OK | X.X fps`
- `[Q]` keluar, `[R]` force resync

Saat match -> `POST /edge/face-match` -> backend buka gate (MQTT) + log
attendance + `POST /edge/events` (update banner halaman `/edge`).

## Auto-sync dari dashboard

Manager tambah karyawan baru (nama + tgl lahir + posisi + foto) di dashboard:
1. Frontend POST `/employees/` + `/employees/{id}/face`
2. Foto disimpan di VPS `backend/uploads/faces/{id}.jpg`
3. Edge server polling tiap 60 detik (`SYNC_EVERY_SECONDS`):
   - Coba `/edge/faces` (AI Gate), fallback `/employees/{id}/faces` (legacy)
   - Download foto, encode 128-d, update `embeddings.npz`
4. Wajah baru langsung dikenali tanpa restart

## Jalan permanen sebagai Windows service

Lihat `docs/SAPA-Kubeadm-Deployment-ID.md` §8.A.6 (NSSM / Task Scheduler).
