# Setup Auto-Deploy GitHub → VPS

Panduan setup supaya setiap `git push` ke `main` otomatis ter-deploy ke VPS.

> **Penanda lokasi:**
> - 🖥️ **[VPS]** = jalankan di terminal VPS
> - 💻 **[LAPTOP]** = jalankan di laptop Anda
> - 🌐 **[GITHUB]** = lakukan di web GitHub

---

## Dua cara update VPS

### Cara A — Manual (paling sederhana, tanpa setup)

Setiap kali mau update, SSH ke VPS dan jalankan satu command:

```bash
# 🖥️ [VPS]
cd /opt/sapa
bash scripts/update-vps.sh
```

Script `update-vps.sh` otomatis: `git pull` → rebuild image (auto-increment tag) →
rolling update → verifikasi `/health` → rollback otomatis kalau gagal.

**Opsi tambahan:**
```bash
# 🖥️ [VPS]
# Hanya update backend (skip frontend)
ONLY=backend bash scripts/update-vps.sh

# Hanya restart tanpa rebuild image (kalau cuma ganti env/config)
SKIP_BUILD=1 bash scripts/update-vps.sh
```

### Cara B — Otomatis via GitHub Actions (push langsung deploy)

Setelah setup sekali (di bawah), cukup `git push` dari laptop → VPS otomatis
ter-update tanpa SSH manual.

---

## Setup Cara B — Auto-Deploy (sekali saja)

### Langkah 1 — Buat SSH key khusus untuk deploy (di VPS)

```bash
# 🖥️ [VPS]
# Buat key pair baru khusus untuk GitHub Actions (jangan pakai key pribadi Anda)
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/gh_deploy -N ""

# Tambahkan public key ke authorized_keys supaya bisa login
cat ~/.ssh/gh_deploy.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

# Tampilkan PRIVATE key (yang akan disimpan di GitHub Secret)
echo "===== COPY SELURUH OUTPUT DI BAWAH (termasuk BEGIN/END) ====="
cat ~/.ssh/gh_deploy
echo "============================================================="
```

**Salin seluruh output** dari `-----BEGIN OPENSSH PRIVATE KEY-----` sampai
`-----END OPENSSH PRIVATE KEY-----`.

### Langkah 2 — Pastikan VPS bisa di-SSH dari internet

```bash
# 🖥️ [VPS]
# Cek IP external VPS
curl -s ifconfig.me; echo
# atau di Google Cloud, lihat di console.cloud.google.com/compute/instances

# Pastikan firewall Google Cloud allow port 22 (SSH).
# Cek aturan firewall:
#   Google Cloud Console -> VPC network -> Firewall -> pastikan allow tcp:22
```

> **Google Cloud:** Buka Console → VPC network → Firewall rules → pastikan ada
> rule yang allow `tcp:22` dari `0.0.0.0/0` (atau dari IP GitHub Actions).
> Biasanya rule `default-allow-ssh` sudah ada.

### Langkah 3 — Tambahkan GitHub Secrets

1. 🌐 Buka repo: `https://github.com/farhanwijayanto/SAPA-Dashboard`
2. **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
3. Tambahkan 4 secret berikut:

| Nama Secret | Nilai | Contoh |
|-------------|-------|--------|
| `VPS_HOST` | IP external VPS | `34.101.123.45` |
| `VPS_USER` | username SSH | `ubuntu` atau `farhan` |
| `VPS_SSH_KEY` | private key dari Langkah 1 | `-----BEGIN OPENSSH...` (seluruh isi) |
| `VPS_SSH_PORT` | port SSH (opsional) | `22` |

### Langkah 4 — Test auto-deploy

```bash
# 💻 [LAPTOP] commit + push apa saja yang menyentuh backend/frontend/k8s
git add .
git commit -m "test auto-deploy"
git push origin main
```

Lalu:
1. 🌐 Buka repo → tab **Actions**
2. Lihat workflow **"Deploy to VPS"** jalan
3. Klik untuk lihat log realtime SSH ke VPS

Atau jalankan **manual** tanpa push:
1. 🌐 Tab Actions → "Deploy to VPS" → tombol **Run workflow** → Run

---

## Alur lengkap auto-deploy

```
💻 [LAPTOP]                🌐 [GITHUB]                    🖥️ [VPS]
git push main  ───────▶  Actions terpicu
                         │
                         ├─ ci.yml: lint + build check
                         │
                         └─ deploy.yml: SSH ke VPS ──────▶ cd /opt/sapa
                                                           git reset --hard origin/main
                                                           bash scripts/update-vps.sh
                                                           ├─ docker build :tag-baru
                                                           ├─ ctr images import
                                                           ├─ kubectl set image
                                                           ├─ rollout status
                                                           └─ verifikasi /health
                                                              (rollback otomatis kalau gagal)
```

---

## Catatan keamanan

| Hal | Catatan |
|-----|---------|
| Private key di GitHub Secret | Aman — GitHub Secrets terenkripsi, tidak muncul di log |
| Key khusus deploy | Pakai key terpisah (`gh_deploy`), bukan key pribadi Anda, supaya gampang di-revoke |
| Revoke akses | Hapus baris `gh_deploy` dari `~/.ssh/authorized_keys` di VPS |
| Firewall | Idealnya batasi port 22 ke IP GitHub Actions, tapi `0.0.0.0/0` umum dipakai |

---

## Troubleshooting auto-deploy

| Gejala (di tab Actions) | Penyebab | Solusi |
|-------------------------|----------|--------|
| `Permission denied (publickey)` | `VPS_SSH_KEY` salah / public key belum di authorized_keys | Ulang Langkah 1 |
| `Connection timed out` | Firewall blok port 22 / IP salah | Cek `VPS_HOST` + firewall Google Cloud |
| `Host key verification failed` | - | Sudah di-handle ssh-action otomatis |
| `git reset --hard` error | Ada perubahan lokal di VPS | SSH manual, `git stash` atau commit dulu |
| `docker build` gagal di VPS | Disk penuh / dependency | SSH manual, jalankan `bash scripts/update-vps.sh` untuk lihat error lengkap |
| Rollout timeout | Pod tidak Ready | Otomatis rollback. Cek `kubectl -n sapa describe pod` |

---

## FAQ

**T: Edge server (laptop) ikut auto-update?**
J: Tidak. Auto-deploy hanya untuk VPS (backend + frontend). Laptop edge update
manual dengan `git pull` + restart (lihat `docs/integrasi.md` §10.2). Ini disengaja
karena laptop edge tidak selalu online dan butuh kontrol manual.

**T: Kalau auto-deploy gagal, dashboard mati?**
J: Tidak. `update-vps.sh` punya rollback otomatis — kalau pod baru gagal Ready,
otomatis kembali ke versi lama. Dashboard tetap jalan dengan versi sebelumnya.

**T: Bisa deploy cuma backend?**
J: Bisa lewat manual: `ONLY=backend bash scripts/update-vps.sh` di VPS.

**T: Push yang tidak menyentuh backend/frontend (mis. docs) ikut deploy?**
J: Tidak. `deploy.yml` hanya terpicu untuk perubahan di `backend/`, `frontend/`,
`k8s/`, atau `scripts/update-vps.sh`. Perubahan docs tidak memicu deploy.
