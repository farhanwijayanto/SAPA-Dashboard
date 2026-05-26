#!/bin/bash
set -e

# Run database seed only when explicitly requested.
# In production set SAPA_RUN_SEED=1 ONLY for the very first deploy or when
# bootstrapping a fresh database. After the initial admin/manager passwords
# have been changed, leave SAPA_RUN_SEED unset so restarts don't reset them.
if [ "${SAPA_RUN_SEED:-0}" = "1" ]; then
  echo "SAPA_RUN_SEED=1 -> seeding database..."
  python -m backend.seed
else
  echo "SAPA_RUN_SEED not set; skipping seed (default for production restarts)."
fi

echo "Starting backend..."
exec uvicorn backend.main:app --host 0.0.0.0 --port 8000
