#!/bin/bash
sleep 5
export PYTHON_VERSION=3.10.13
gunicorn keepalive:app --bind 0.0.0.0:$PORT &
python3 bot.py
