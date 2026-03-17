#!/bin/bash


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

echo "[*] Starting services..."
pgrep -x "php-fpm8.4" > /dev/null || php-fpm8.4 &
pgrep -x "nginx" > /dev/null || /usr/sbin/nginx &
sed -i 's|/usr/share/nginx/html|/var/www/html|g' /etc/nginx/conf.d/default.conf
service nginx restart

if ! ss -tuln | grep -q ":8080 "; then
  su - sugarush -c "python3 /home/sugarush/webservice.py &"
 else echo "Port 8080 already in use";
fi

sleep infinity
