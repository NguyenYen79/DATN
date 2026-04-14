#!/bin/bash

echo "[START] Khởi động Camera Service..."
source venv/bin/activate
python camera.py &

echo "[START] Khởi động Web Server..."
python run.py