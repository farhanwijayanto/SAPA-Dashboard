#!/usr/bin/env bash
# bootstrap-secrets.sh
#
# Idempotent script untuk men-generate semua password & secret yang dibutuhkan
# SAPA, lalu meng-apply-nya ke cluster Kubernetes (kubeadm/k8s).
#
# Yang dihandle:
#   1. Pastikan namespace 'sapa' ada
#   2. Generate password Postgres, MQTT (backend/esp32/edge), SECRET_KEY,
#      EDGE_INGEST_KEY, dan password awal admin/manager — kecuali secret
#      yang sudah ada di cluster (existing values dipertahankan supaya
#      aplikasi yang sudah jalan TIDAK rusak)
#   3. Apply Secret 'sapa-db-secret', 'sapa-backend-secret',
#      'sapa-mosquitto-auth'
#   4. Tulis backup plaintext semua nilai ke file lokal
#      'sapa-secrets-backup-<timestamp>.txt' (mode 600). Pindahkan file ini
#      ke password manager Anda lalu HAPUS.
#
# Cara pakai (dari root repo, di VPS):
#   chmod +x k8s/bootstrap-secrets.sh
#   ./k8s/bootstrap-secrets.sh                # interactive, generate semua baru
#   FORCE_REGENERATE=1 ./k8s/bootstrap-secrets.sh   # overwrite existing
#   DRY_RUN=1 ./k8s/bootstrap-secrets.sh      # tampilkan tanpa apply
#
# Requirement di VPS:
#   - kubectl (config sudah point ke cluster sapa)
#   - openssl
#   - docker (untuk generate hash mosquitto via image eclipse-mosquitto:2)

set -euo pipefail

NAMESPACE="${NAMESPACE:-sapa}"
DRY_RUN="${DRY_RUN:-0}"
FORCE_REGENERATE="${FORCE_REGENERATE:-0}"
BACKUP_FILE="sapa-secrets-backup-$(date +%Y%m%d-%H%M%S).txt"

# ----- helpers --------------------------------------------------------------

log() { printf '\033[1;36m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "command not found: $1"
}

require_cmd kubectl
require_cmd openssl
require_cmd docker

# Generate alfanumerik kuat (no symbol biar aman di YAML/connection string)
gen_alnum() {
  local len="${1:-24}"
  openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "$len"
}

gen_hex() {
  openssl rand -hex "${1:-32}"
}

# Cari nilai existing dari Secret. Echo plaintext kalau ada, atau "" kalau tidak.
read_existing() {
  local secret_name="$1"
  local key="$2"
  kubectl -n "$NAMESPACE" get secret "$secret_name" \
    -o jsonpath="{.data.$key}" 2>/dev/null \
    | { base64 -d 2>/dev/null || true; } \
    | tr -d '\r\n'
}

# Reuse existing kalau ada (kecuali FORCE_REGENERATE=1), kalau tidak generate baru
ensure_value() {
  local secret_name="$1"
  local key="$2"
  local generator_cmd="$3"
  local existing
  existing="$(read_existing "$secret_name" "$key" || true)"
  if [[ -n "$existing" && "$FORCE_REGENERATE" != "1" ]]; then
    printf '%s' "$existing"
  else
    eval "$generator_cmd"
  fi
}

apply_or_dry() {
  local manifest="$1"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN=1 — manifest yang akan di-apply:"
    printf '%s\n' "$manifest"
  else
    printf '%s' "$manifest" | kubectl apply -f -
  fi
}

# ----- ensure namespace -----------------------------------------------------

if ! kubectl get ns "$NAMESPACE" >/dev/null 2>&1; then
  log "Namespace '$NAMESPACE' belum ada — membuat..."
  if [[ "$DRY_RUN" != "1" ]]; then
    kubectl create namespace "$NAMESPACE"
  fi
else
  log "Namespace '$NAMESPACE' sudah ada."
fi

# ----- generate / reuse semua nilai -----------------------------------------

log "Mengumpulkan nilai secret (reuse existing kalau ada)..."

PG_USER="$(ensure_value sapa-db-secret POSTGRES_USER 'echo sapa')"
PG_DB="$(ensure_value sapa-db-secret POSTGRES_DB 'echo sapa_db')"
PG_PASSWORD="$(ensure_value sapa-db-secret POSTGRES_PASSWORD 'gen_alnum 24')"

SECRET_KEY="$(ensure_value sapa-backend-secret SECRET_KEY 'gen_hex 32')"
EDGE_INGEST_KEY="$(ensure_value sapa-backend-secret EDGE_INGEST_KEY 'gen_hex 24')"
MQTT_USERNAME="$(ensure_value sapa-backend-secret MQTT_USERNAME 'echo backend')"
MQTT_BACKEND_PW="$(ensure_value sapa-backend-secret MQTT_PASSWORD 'gen_alnum 18')"
SAPA_ADMIN_PW="$(ensure_value sapa-backend-secret SAPA_ADMIN_PASSWORD 'gen_alnum 14')"
SAPA_MANAGER_PW="$(ensure_value sapa-backend-secret SAPA_MANAGER_PASSWORD 'gen_alnum 14')"

# Untuk mosquitto-auth kita TIDAK bisa reuse plaintext karena yang tersimpan di
# secret hanya hash (file passwords). Jadi:
#  - kalau secret 'sapa-mosquitto-auth' SUDAH ADA dan FORCE_REGENERATE!=1,
#    biarkan apa adanya (skip regenerate)
#  - kalau belum ada, generate password esp32/edge baru lalu hash-kan
MOSQUITTO_REGEN=0
if ! kubectl -n "$NAMESPACE" get secret sapa-mosquitto-auth >/dev/null 2>&1; then
  MOSQUITTO_REGEN=1
elif [[ "$FORCE_REGENERATE" == "1" ]]; then
  warn "FORCE_REGENERATE=1 — Mosquitto password akan di-generate ulang."
  MOSQUITTO_REGEN=1
fi

if [[ "$MOSQUITTO_REGEN" == "1" ]]; then
  MQTT_ESP32_PW="$(gen_alnum 18)"
  MQTT_EDGE_PW="$(gen_alnum 18)"
  log "Hashing Mosquitto password via eclipse-mosquitto:2..."
  TMP_PW=$(mktemp)
  trap 'rm -f "$TMP_PW"' EXIT

  docker run --rm \
    -v "$TMP_PW:/tmp/passwords" \
    eclipse-mosquitto:2 \
    sh -c "
      mosquitto_passwd -c -b /tmp/passwords backend '$MQTT_BACKEND_PW' &&
      mosquitto_passwd -b /tmp/passwords esp32   '$MQTT_ESP32_PW' &&
      mosquitto_passwd -b /tmp/passwords edge    '$MQTT_EDGE_PW'
    " >/dev/null

  MOSQUITTO_PASSWORDS_FILE_CONTENT="$(cat "$TMP_PW")"
else
  MQTT_ESP32_PW="<existing — tidak diubah>"
  MQTT_EDGE_PW="<existing — tidak diubah>"
fi

# ----- assemble + apply Secrets ---------------------------------------------

log "Apply secret 'sapa-db-secret'..."
DB_MANIFEST=$(cat <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: sapa-db-secret
  namespace: $NAMESPACE
type: Opaque
stringData:
  POSTGRES_USER: "$PG_USER"
  POSTGRES_PASSWORD: "$PG_PASSWORD"
  POSTGRES_DB: "$PG_DB"
EOF
)
apply_or_dry "$DB_MANIFEST"

log "Apply secret 'sapa-backend-secret'..."
BACKEND_MANIFEST=$(cat <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: sapa-backend-secret
  namespace: $NAMESPACE
type: Opaque
stringData:
  DATABASE_URL: "postgresql://$PG_USER:$PG_PASSWORD@sapa-db:5432/$PG_DB"
  MONGODB_URI: "mongodb://sapa-mongo:27017"
  SECRET_KEY: "$SECRET_KEY"
  EDGE_INGEST_KEY: "$EDGE_INGEST_KEY"
  MQTT_USERNAME: "$MQTT_USERNAME"
  MQTT_PASSWORD: "$MQTT_BACKEND_PW"
  SAPA_ADMIN_PASSWORD: "$SAPA_ADMIN_PW"
  SAPA_MANAGER_PASSWORD: "$SAPA_MANAGER_PW"
EOF
)
apply_or_dry "$BACKEND_MANIFEST"

if [[ "$MOSQUITTO_REGEN" == "1" ]]; then
  log "Apply secret 'sapa-mosquitto-auth'..."
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN=1 — sapa-mosquitto-auth tidak di-apply."
  else
    kubectl -n "$NAMESPACE" delete secret sapa-mosquitto-auth --ignore-not-found
    kubectl -n "$NAMESPACE" create secret generic sapa-mosquitto-auth \
      --from-literal=passwords="$MOSQUITTO_PASSWORDS_FILE_CONTENT"
  fi
else
  log "sapa-mosquitto-auth sudah ada — skip (pakai FORCE_REGENERATE=1 untuk overwrite)."
fi

# ----- backup plaintext ke file lokal ---------------------------------------

if [[ "$DRY_RUN" == "1" ]]; then
  log "DRY_RUN=1 — file backup TIDAK dibuat."
else
  log "Menulis backup plaintext ke '$BACKUP_FILE' (mode 600)..."
  umask 077
  cat > "$BACKUP_FILE" <<EOF
SAPA Secret Backup
Generated: $(date -Iseconds)
Cluster:   $(kubectl config current-context 2>/dev/null || echo unknown)
Namespace: $NAMESPACE

PINDAHKAN file ini ke password manager / vault Anda, lalu HAPUS.

# --- Postgres ---
POSTGRES_USER=$PG_USER
POSTGRES_PASSWORD=$PG_PASSWORD
POSTGRES_DB=$PG_DB
DATABASE_URL=postgresql://$PG_USER:$PG_PASSWORD@sapa-db:5432/$PG_DB

# --- Backend / Auth ---
SECRET_KEY=$SECRET_KEY
EDGE_INGEST_KEY=$EDGE_INGEST_KEY

# --- Login awal (boleh di-rotate via UI sesudah login pertama) ---
SAPA_ADMIN_PASSWORD=$SAPA_ADMIN_PW
SAPA_MANAGER_PASSWORD=$SAPA_MANAGER_PW

# --- MQTT ---
MQTT_USERNAME=$MQTT_USERNAME
MQTT_PASSWORD=$MQTT_BACKEND_PW
MQTT_ESP32_PASSWORD=$MQTT_ESP32_PW
MQTT_EDGE_PASSWORD=$MQTT_EDGE_PW
EOF
  chmod 600 "$BACKUP_FILE"
fi

# ----- ringkasan + langkah berikutnya ---------------------------------------

log "Selesai."
cat <<EOF

Ringkasan:
  Namespace                  : $NAMESPACE
  sapa-db-secret             : applied
  sapa-backend-secret        : applied
  sapa-mosquitto-auth        : $([[ "$MOSQUITTO_REGEN" == "1" ]] && echo "regenerated" || echo "kept (existing)")
  Backup plaintext (lokal)   : $([[ "$DRY_RUN" == "1" ]] && echo "skipped (DRY_RUN)" || echo "$BACKUP_FILE")

Langkah berikutnya:
  1. Pindahkan '$BACKUP_FILE' ke password manager, lalu hapus dari VPS.
  2. (Pertama kali) Apply seed Job:
       kubectl apply -f k8s/seed-job.yaml
       kubectl -n $NAMESPACE wait --for=condition=complete job/sapa-backend-seed --timeout=120s
       kubectl -n $NAMESPACE logs job/sapa-backend-seed
  3. Restart backend supaya pickup secret terbaru:
       kubectl -n $NAMESPACE rollout restart deploy/sapa-backend
       kubectl -n $NAMESPACE rollout restart deploy/sapa-mosquitto
  4. Verifikasi:
       kubectl -n $NAMESPACE get pods
       kubectl -n $NAMESPACE logs deploy/sapa-backend --tail=30
EOF
