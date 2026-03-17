#!/bin/bash

echo "[*] Starting services..."
pgrep -x "php-fpm8.4" > /dev/null || php-fpm8.4 &
pgrep -x "nginx" > /dev/null || /usr/sbin/nginx &
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

sleep infinity
