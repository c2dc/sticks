#!/bin/bash

echo "[*] Starting services..."
pgrep -x "php-fpm8.4" > /dev/null || php-fpm8.4 &
pgrep -x "nginx" > /dev/null || /usr/sbin/nginx &
sed -i 's|/usr/share/nginx/html|/var/www/html|g' /etc/nginx/conf.d/default.conf

cat > /var/www/html/index.html << 'EOF'
<!DOCTYPE html>
<html>
<head>
    <title>Welcome to My Server</title>
</head>
<body>
    <h1>Hello, World!</h1>
    <p>This is a test page.</p>
</body>
</html>
EOF

service nginx restart
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