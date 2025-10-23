#!/usr/bin/env bash
set -e
echo "Starting hyper-bot (gunicorn)…"
exec gunicorn -b 0.0.0.0:${PORT:-10000} bot:app --workers=1 --threads=4 --timeout=120
