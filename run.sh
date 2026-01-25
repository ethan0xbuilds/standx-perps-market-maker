#!/bin/bash
set -e
cd "$(dirname "$0")"

# Check virtualenv and config
[ -d ".venv" ] || { echo "virtualenv not found"; exit 1; }
[ -f ".env" ] || { echo ".env not found"; exit 1; }

source .venv/bin/activate
mkdir -p logs

# Install dependencies (ensure runtime deps present)
echo "Installing dependencies..."
.venv/bin/python -m pip install -r requirements.txt >/dev/null || { echo "Dependency installation failed"; exit 1; }

# Note: logging and rotation are handled by the Python application (logger.py).
# This script starts the app in the foreground so systemd/journal can capture stdout/stderr.

# Prevent multiple instances
pgrep -f "python.*market_maker.py" > /dev/null && { 
    echo "Already running"
    exit 1
}

echo "Starting Market Maker..."
exec .venv/bin/python -u market_maker.py
