#!/bin/bash
# db

echo "[*] Initializing campaign environments..."

/apt41_dust_sutb.sh
/c0010_sutb.sh
/c0026_sutb.sh
/costaricto_sutb.sh
/operation_midnighteclipse_sutb.sh
/outer_space_sutb.sh
/salesforce_data_exfiltration_sutb.sh
/shadowray_sutb.sh




mkdir -p /var/www/html/
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

sed -i 's/^user[[:space:]]\+nginx;/user www-data;/' /etc/nginx/nginx.conf

cat > /etc/nginx/conf.d/default.conf << 'EOF'
server {
    listen       80;
    server_name  localhost;

    #access_log  /var/log/nginx/host.access.log  main;

    location / {
        root   /var/www/html;
        index  index.html index.htm;
    }

    error_page   500 502 503 504  /50x.html;
    location = /50x.html {
        root   /var/www/html;
    }

    location ~ \.php$ {
        root           /var/www/html;
        fastcgi_pass   unix:/run/php/php8.4-fpm.sock;
        fastcgi_index  index.php;
        fastcgi_param  SCRIPT_FILENAME  $document_root$fastcgi_script_name;
        include        fastcgi_params;
    }

    #location ~ /\.ht {
    #    deny  all;
    #}
}
EOF

cat > /var/www/html/50x.html << 'EOF'
<!DOCTYPE html>
<html>
<head>
<title>Error</title>
<style>
html { color-scheme: light dark; }
body { width: 35em; margin: 0 auto;
font-family: Tahoma, Verdana, Arial, sans-serif; }
</style>
</head>
<body>
<h1>An error occurred.</h1>
<p>Sorry, the page you are looking for is currently unavailable.<br/>
Please try again later.</p>
<p>If you are the system administrator of this resource then you should check
the error log for details.</p>
<p><em>Faithfully yours, nginx.</em></p>
</body>
</html>
EOF


echo "[*] Starting services..."


pgrep -x "php-fpm8.4" > /dev/null || php-fpm8.4 &
pgrep -x "nginx" > /dev/null || /usr/sbin/nginx &
service nginx reload

echo "[*] Environment ready."

sleep infinity