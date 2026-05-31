#!/usr/bin/env bash
# =============================================================================
# update-vps.sh — Update SAPA di VPS (pull GitHub -> rebuild -> rolling update)
# =============================================================================
# Jalankan DI VPS:
#   cd /opt/sapa
#   bash scripts/update-vps.sh
#
# Atau sekali set executable lalu panggil langsung:
#   chmod +x scripts/update-vps.sh
#   ./scripts/update-vps.sh
#
# Yang dilakukan:
#   1. git pull origin main
#   2. Deteksi versi image berikutnya (auto-increment tag)
#   3. docker build backend + frontend
#   4. Import image ke containerd (k8s.io namespace)
#   5. kubectl set image (rolling update, zero downtime)
#   6. Tunggu rollout + verifikasi /health
#   7. Rollback otomatis kalau rollout gagal
#
# Opsi via env var:
#   SKIP_BUILD=1     -> hanya git pull + restart (tanpa rebuild image)
#   ONLY=backend     -> hanya update backend (atau frontend)
#   NO_ROLLBACK=1    -> jangan auto-rollback saat gagal
# =============================================================================

set -euo pipefail

NAMESPACE="sapa"
REPO_DIR="/opt/sapa"
ONLY="${ONLY:-}"            # backend | frontend | (kosong = keduanya)
SKIP_BUILD="${SKIP_BUILD:-0}"
NO_ROLLBACK="${NO_ROLLBACK:-0}"

# ----- helpers --------------------------------------------------------------
c_green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
c_yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
c_red() { printf '\033[1;31m%s\033[0m\n' "$*"; }
c_blue() { printf '\033[1;36m%s\033[0m\n' "$*"; }
die() { c_red "[ERROR] $*"; exit 1; }

cd "$REPO_DIR" 2>/dev/null || die "Repo tidak ditemukan di $REPO_DIR"

command -v git >/dev/null || die "git tidak terpasang"
command -v docker >/dev/null || die "docker tidak terpasang"
command -v kubectl >/dev/null || die "kubectl tidak terpasang"

# ----- 1. git pull ----------------------------------------------------------
c_blue "==> [1/6] git pull origin main"
BEFORE=$(git rev-parse --short HEAD)
git pull origin main
AFTER=$(git rev-parse --short HEAD)

if [[ "$BEFORE" == "$AFTER" ]]; then
  c_yellow "Tidak ada commit baru (HEAD tetap $AFTER)."
  c_yellow "Lanjut rebuild+deploy untuk memastikan cluster sinkron..."
else
  c_green "Update $BEFORE -> $AFTER"
fi

# ----- 2. tentukan tag image berikutnya -------------------------------------
# Ambil tag image backend yang sedang berjalan, lalu increment minor.
CURRENT_IMG=$(kubectl -n "$NAMESPACE" get deploy sapa-backend \
  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "sapa-backend:1.2")
CURRENT_TAG="${CURRENT_IMG##*:}"

# Increment: 1.2 -> 1.3 ; 1.9 -> 1.10
if [[ "$CURRENT_TAG" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
  MAJOR="${BASH_REMATCH[1]}"
  MINOR="${BASH_REMATCH[2]}"
  NEW_TAG="${MAJOR}.$((MINOR + 1))"
else
  # fallback: pakai timestamp
  NEW_TAG="$(date +%Y%m%d%H%M)"
fi
c_blue "==> Tag image baru: $NEW_TAG (sebelumnya $CURRENT_TAG)"

build_and_load() {
  local comp="$1"   # backend | frontend
  local tag="$2"
  c_blue "==> Building sapa-${comp}:${tag}"
  docker build --network=host -t "sapa-${comp}:${tag}" "./${comp}"
  c_blue "==> Import sapa-${comp}:${tag} ke containerd"
  docker save "sapa-${comp}:${tag}" | sudo ctr -n k8s.io images import -
}

rollout_component() {
  local comp="$1"   # backend | frontend
  local tag="$2"
  c_blue "==> Rolling update deploy/sapa-${comp} -> sapa-${comp}:${tag}"
  kubectl -n "$NAMESPACE" set image "deploy/sapa-${comp}" "${comp}=sapa-${comp}:${tag}"
  if ! kubectl -n "$NAMESPACE" rollout status "deploy/sapa-${comp}" --timeout=5m; then
    c_red "Rollout sapa-${comp} GAGAL."
    if [[ "$NO_ROLLBACK" != "1" ]]; then
      c_yellow "Rollback otomatis sapa-${comp}..."
      kubectl -n "$NAMESPACE" rollout undo "deploy/sapa-${comp}" || true
      kubectl -n "$NAMESPACE" rollout status "deploy/sapa-${comp}" --timeout=3m || true
    fi
    return 1
  fi
  c_green "Rollout sapa-${comp} OK"
}

# ----- 3-5. build + deploy --------------------------------------------------
FAILED=0

do_component() {
  local comp="$1"
  if [[ "$SKIP_BUILD" == "1" ]]; then
    c_yellow "SKIP_BUILD=1 -> restart deploy/sapa-${comp} saja"
    kubectl -n "$NAMESPACE" rollout restart "deploy/sapa-${comp}"
    kubectl -n "$NAMESPACE" rollout status "deploy/sapa-${comp}" --timeout=5m || FAILED=1
  else
    build_and_load "$comp" "$NEW_TAG"
    rollout_component "$comp" "$NEW_TAG" || FAILED=1
  fi
}

if [[ -z "$ONLY" || "$ONLY" == "backend" ]]; then
  do_component backend
fi
if [[ -z "$ONLY" || "$ONLY" == "frontend" ]]; then
  do_component frontend
fi

# ----- 6. verifikasi --------------------------------------------------------
c_blue "==> [6/6] Verifikasi"
kubectl -n "$NAMESPACE" get pods -o wide || true

HEALTH=$(curl -fsS https://sapa.farhn.dev/api/health 2>/dev/null || echo "FAIL")
if [[ "$HEALTH" == *"ok"* ]]; then
  c_green "Health check OK: $HEALTH"
else
  c_red "Health check GAGAL (response: $HEALTH)"
  FAILED=1
fi

if [[ "$FAILED" == "1" ]]; then
  c_red "================================================="
  c_red " UPDATE SELESAI DENGAN ERROR. Cek log di atas."
  c_red " Manual rollback: kubectl -n $NAMESPACE rollout undo deploy/sapa-backend"
  c_red "================================================="
  exit 1
fi

c_green "================================================="
c_green " UPDATE BERHASIL -> commit $AFTER, image tag $NEW_TAG"
c_green " Dashboard: https://sapa.farhn.dev"
c_green "================================================="
