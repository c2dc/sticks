############### SALESFORCE DATA EXFILTRATION
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


################################