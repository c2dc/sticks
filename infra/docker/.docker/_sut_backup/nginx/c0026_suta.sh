############### C0026

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

# T1030
# none

# T1568
if ! grep -q "zone \"dynamic.local\"" /etc/bind/named.conf.local; then
    cat >> /etc/bind/named.conf.local << "EOF"

zone "dynamic.local" {
    type master;
    file "/etc/bind/db.dynamic.local";
};
EOF
fi
cat > /etc/bind/db.dynamic.local << "EOF"
$TTL 604800
@ IN SOA ns1.dynamic.local. admin.dynamic.local. (
    2024031501  ; Serial
    604800      ; Refresh
    86400       ; Retry
    2419200     ; Expire
    604800 )    ; Negative Cache TTL
;
@ IN NS ns1.dynamic.local.
ns1 IN A 172.21.0.20
hostb IN A 172.22.0.20
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

# T1560.001
apt-get install zip


# T1583.001
apt-get install -y bind9 dnsutils

if ! grep -q "zone \"malicious.example\"" /etc/bind/named.conf.local; then
    cat >> /etc/bind/named.conf.local << "EOF"
zone "malicious.example" {
    type master;
    file "/etc/bind/db.malicious.example";
};
EOF
fi

cat > /etc/bind/db.malicious.example << 'EOF'
$TTL    604800
@       IN      SOA     ns.malicious.example. root.malicious.example. (
                              2         ; Serial
                         604800         ; Refresh
                          86400         ; Retry
                        2419200         ; Expire
                         604800 )       ; Negative Cache TTL
;
@       IN      NS      ns.malicious.example.
ns      IN      A       172.21.0.20
@       IN      A       172.21.0.20
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

# T1105
echo "bad file" > /var/www/html/maliciousfile.sh


