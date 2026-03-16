#!/bin/bash

echo "[*] Starting services..."
php-fpm8.4 -D
nginx

echo "[*] Initializing campaign environments..."

/apt41_dust_suta.sh
/c0010_suta.sh
/c0026_suta.sh
/costaricto_suta.sh
/operation_midnighteclipse_suta.sh
/outer_space_suta.sh
/salesforce_data_exfiltration_suta.sh
/shadowray_suta.sh

echo "[*] Environment ready."

nginx -g "daemon off;"
