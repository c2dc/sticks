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

echo "[*] Starting services..."
pgrep -x "php-fpm8.4" > /dev/null || php-fpm8.4 &
pgrep -x "nginx" > /dev/null || /usr/sbin/nginx &
sed -i 's|/usr/share/nginx/html|/var/www/html|g' /etc/nginx/conf.d/default.conf
service nginx restart

if ! ss -tuln | grep -q ":8080 "; then
  su - sugarush -c "python3 /home/sugarush/webservice.py &"
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
