#!/bin/bash

# Karaoke ni Lakay Headless Mode Verification Script (Unix)
# This script starts Karaoke ni Lakay in headless mode, waits for initialization,
# and verifies that key web endpoints are serving content.

set -e

echo "Installing Karaoke ni Lakay for CI..."
./build_scripts/install/install.sh -y --local

echo "Starting Karaoke ni Lakay in headless mode..."
pikaraoke --headless --skip-youtubedl-upgrade > output.log 2>&1 &
PID=$!

# Function to cleanup on exit
cleanup() {
    echo "Cleaning up..."
    kill $PID || true
}
trap cleanup EXIT

echo "Waiting for Karaoke ni Lakay to initialize (max 30s)..."
INITIALIZED=false
for i in {1..30}; do
    if grep -q "Connect the player host to:" output.log; then
        echo "Found expected initialization output."
        INITIALIZED=true
        break
    fi
    sleep 1
done

if [ "$INITIALIZED" = false ]; then
    echo "Error: Timed out waiting for Karaoke ni Lakay to initialize."
    cat output.log
    exit 1
fi

echo "Verifying web endpoints..."
ENDPOINTS=(
    "/"
    "/splash"
    "/queue"
    "/search"
    "/browse"
    "/info"
)

FAILED=false
for path in "${ENDPOINTS[@]}"; do
    echo "Checking http://localhost:5555$path ..."
    if ! curl -s http://localhost:5555"$path" | grep -q "DOCTYPE"; then
        echo "Error: Failed to verify $path"
        FAILED=true
    fi
done

if [ "$FAILED" = true ]; then
    echo "One or more endpoint verifications failed."
    cat output.log
    exit 1
fi

echo "Headless mode verification successful!"
exit 0
