#!/bin/bash

echo "[*] Starting services..."
php-fpm8.4 -D
nginx

echo "[*] Initializing campaign environments..."

/apt41_dust_sutb.sh
/c0010_sutb.sh
/c0026_sutb.sh
/costaricto_sutb.sh
/operation_midnighteclipse_sutb.sh
/outer_space_sutb.sh
/salesforce_data_exfiltration_sutb.sh
/shadowray_sutb.sh

echo "[*] Environment ready."

nginx -g "daemon off;"
