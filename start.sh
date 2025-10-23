#!/usr/bin/env bash
set -e

# Start keepalive web server in background
gunicorn keepalive:app --bind 0.0.0.0:${PORT} &

# Delay 2 seconds to make sure webserver boots
sleep 2

# Start the bot loop
python bot.py
