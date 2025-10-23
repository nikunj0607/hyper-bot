#!/bin/bash
gunicorn bot:app --bind 0.0.0.0:$PORT
