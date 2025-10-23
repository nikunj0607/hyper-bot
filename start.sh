#!/usr/bin/env bash
set -e
# Start mini web server to keep Render awake
gunicorn keepalive:app --bind 0.0.0.0:${PORT} &

# Start the bot
python bot.py
