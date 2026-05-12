#!/bin/bash
set -e

echo "Seeding database..."
python -m backend.seed

echo "Starting backend..."
exec uvicorn backend.main:app --host 0.0.0.0 --port 8000
