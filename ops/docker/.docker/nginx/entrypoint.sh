#!/bin/bash
#nginx

echo "[*] Initializing campaign environments..."

/apt41_dust_suta.sh 
/c0010_suta.sh 
/c0026_suta.sh 
/costaricto_suta.sh 
/operation_midnighteclipse_suta.sh 
/outer_space_suta.sh 
/salesforce_data_exfiltration_suta.sh 
/shadowray_suta.sh 



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

if ! ss -tuln | grep -q ":8080 "; then
  su - attacker -c "python3 /home/attacker/webservice.py &"
 else echo "Port 8080 already in use";
fi

cat > /var/www/html/tool.bin << 'EOF'
This is a binary file placeholder
EOF

cat > /var/www/html/tool.sh << 'EOF'
#!/bin/bash
echo "This is a shell script"
EOF

chmod +x /var/www/html/tool.sh

echo "[*] Environment ready."

sleep infinity
