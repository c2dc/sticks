############### APT 41 DUST

# T1102 - Web Service
apt-get update
apt-get install -y python3 python3-pip
pip install --break-system-packages flask requests
cat << 'EOF' > /root/webservice.py
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
python3 /root/webservice.py &


# T1213.006 - Databases

apt-get install -y mariadb-server
sed -i 's/^bind-address\s*=.*/bind-address = 0.0.0.0/' /etc/mysql/mariadb.conf.d/50-server.cnf
mariadbd-safe &
sleep 5

mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED WITH mysql_native_password BY 'RootPassw0rd!'; FLUSH PRIVILEGES;"
mysql -u root -pRootPassw0rd\! -e "CREATE DATABASE sensitive_data;"
mysql -e "CREATE USER 'attacker'@'%' IDENTIFIED BY 'Attack3rPass!';"
mysql -e "GRANT ALL PRIVILEGES ON sensitive_data.* TO 'attacker'@'%'; FLUSH PRIVILEGES;"
mysql -e "USE sensitive_data; CREATE TABLE credentials(id INT AUTO_INCREMENT PRIMARY KEY, username VARCHAR(50), password VARCHAR(255));"
mysql -e "INSERT INTO sensitive_data.credentials(username,password) VALUES ('admin','5f4dcc3b5aa765d61d8327deb882cf99');"
service mariadb restart


