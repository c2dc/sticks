# ============================================================
# SUT-B — alvo backend (192.168.30.40)
# ============================================================
# Mesma motivação do SUT-A (nginx): os scripts *_sutb.sh instalavam pacotes em
# runtime via apt-get, o que falha nas redes `internal: true` (sem internet).
# Instalação movida para o BUILD-TIME (ver LAB.md). Em runtime os scripts só
# configuram e iniciam serviços. Apesar do nome "db", a imagem base é nginx e o
# container também serve conteúdo web (porta 80) além do MariaDB.
# ============================================================
FROM nginx:trixie

ENV DEBIAN_FRONTEND=noninteractive

# Pacotes base + superconjunto exigido por TODOS os scripts *_sutb.sh dos 8 casos.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        iputils-ping procps lsof net-tools vim iproute2 \
        nginx php-fpm php8.4-fpm php-mysql php-curl php-gd php-mbstring php-xml php-zip \
        python3 python3-pip python3-venv \
        openssh-server sshpass \
        curl wget socat netcat-openbsd \
        mariadb-server \
        samba samba-common-bin \
        sudo openssl unzip zip p7zip-full \
        bind9 dnsutils nmap tinyproxy \
        git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Dependências Python (superconjunto) instaladas offline-ready no build.
RUN pip3 install --break-system-packages --no-cache-dir \
        flask flask-cors requests paramiko boto3 psutil schedule pycryptodome

# Copy campaign setup scripts
COPY apt41_dust_sutb.sh /apt41_dust_sutb.sh
COPY c0010_sutb.sh /c0010_sutb.sh
COPY c0026_sutb.sh /c0026_sutb.sh
COPY costaricto_sutb.sh /costaricto_sutb.sh
COPY operation_midnighteclipse_sutb.sh /operation_midnighteclipse_sutb.sh
COPY outer_space_sutb.sh /outer_space_sutb.sh
COPY salesforce_data_exfiltration_sutb.sh /salesforce_data_exfiltration_sutb.sh
COPY shadowray_sutb.sh /shadowray_sutb.sh

# Normalize line endings (strip CRLF from Windows-cloned repo) and make executable
RUN sed -i 's/\r$//' /*_sutb.sh && chmod +x /*_sutb.sh

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
# Normalize line endings (strip CRLF from Windows-cloned repo) and make executable
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]