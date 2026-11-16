#!/bin/sh
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 не найден. Установи: sudo apt install python3 python3-tk"
    exit 1
fi
exec python3 LitLauncher.py
