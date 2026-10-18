############### COSTARICTO
# Global
apt-get update
apt-get -y install mariadb-server

sed -i 's/^bind-address\s*=.*/bind-address = 0.0.0.0/' /etc/mysql/mariadb.conf.d/50-server.cnf
pgrep -x "mariadbd-safe" > /dev/null || mariadbd-safe &
sleep 5
mysql -u root -e "ALTER USER 'root'@'localhost' IDENTIFIED BY 'RootPassw0rd';FLUSH PRIVILEGES;"
mysql -u root -pRootPassw0rd -e "CREATE DATABASE sensitive_data;"
mysql -u root -pRootPassw0rd -e "CREATE USER 'attacker'@'%' IDENTIFIED BY 'Attack3rPass';"
mysql -u root -pRootPassw0rd -e "GRANT ALL PRIVILEGES ON sensitive_data.* TO 'attacker'@'%'; FLUSH PRIVILEGES;"
mysql -u root -pRootPassw0rd -e "USE sensitive_data; CREATE TABLE credentials(id INT AUTO_INCREMENT PRIMARY KEY, username VARCHAR(50), password VARCHAR(255));"
mysql -u root -pRootPassw0rd -e "INSERT INTO sensitive_data.credentials(username,password) VALUES ('admin','5f4dcc3b5aa765d61d8327deb882cf99');"


# nginx
apt-get install -y nginx php8.4-fpm
pgrep -x "php-fpm8.4" > /dev/null || php-fpm8.4 &
pgrep -x "nginx" > /dev/null || /usr/sbin/nginx &

## SSH
apt-get update
apt-get install -y -o Dpkg::Options::="--force-confnew" openssh-server sshpass
mkdir -p /var/run/sshd
echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config
echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config

pgrep -x "sshd" > /dev/null || /usr/sbin/sshd & 


# Root password
echo 'root:RootPass123' | chpasswd

# User attacker

useradd -m attacker -s /bin/bash
mkdir -p /home/attacker
echo 'attacker:Passw0rd' | chpasswd
usermod -aG sudo attacker

# T1046
apt-get install -y python3
mkdir -p /tmp/tools
chown root:root /tmp/tools

# T1090.003

apt-get install -y tinyproxy
cat > /etc/tinyproxy/tinyproxy.conf << 'EOF'
Port 8888
Listen 0.0.0.0
Allow 172.22.0.20
ALLOW 172.21.0.10
Allow 172.21.0.20
EOF
if ! ss -tuln | grep -q ":8888 "; 
 then /usr/bin/tinyproxy -c /etc/tinyproxy/tinyproxy.conf &
 else echo "Port 8888 already in use";
 fi

# T1105
apt-get install -y python3 python3-pip python3-venv curl netcat-openbsd
mkdir -p /home/attacker/tools
chown attacker:attacker /home/attacker/tools
cat > /home/attacker/tools/simple_http_server.py << 'EOF'
#!/usr/bin/env python3

import http.server
import socketserver
import sys

HOST = "0.0.0.0"
PORT = 8018

Handler = http.server.SimpleHTTPRequestHandler

try:
    with socketserver.TCPServer((HOST, PORT), Handler) as httpd:
        print(f"[*] HTTP server running on {HOST}:{PORT}")
        httpd.serve_forever()
except OSError:
    print(f"[!] Port {PORT} is already in use.")
    sys.exit(1)
except KeyboardInterrupt:
    print("\n[*] Server stopped.")
EOF
chmod +x /home/attacker/tools/simple_http_server.py

if ! ss -tuln | grep -q ":8018 "; then
    su attacker -c "python3 /home/attacker/tools/simple_http_server.py &"
else
    echo "Port 8018 already in use"
fi

