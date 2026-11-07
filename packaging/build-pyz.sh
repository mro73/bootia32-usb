#!/bin/sh
# Builds dist/bootia32-usb.pyz : ONE file containing the app + bootia32.efi.
# Run it with:  python3 bootia32-usb.pyz   (or ./bootia32-usb.pyz)
set -eu
cd "$(dirname "$0")/.."
rm -rf build/pyz && mkdir -p build/pyz/data dist
cp bootia32-usb.py build/pyz/__main__.py
cp data/bootia32.efi data/CREDITS.txt build/pyz/data/
python3 -m zipapp build/pyz -o dist/bootia32-usb.pyz -p "/usr/bin/env python3" -c
chmod +x dist/bootia32-usb.pyz
echo "Built dist/bootia32-usb.pyz ($(du -h dist/bootia32-usb.pyz | cut -f1))"
