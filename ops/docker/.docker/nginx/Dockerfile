# ============================================================
# SUT-A — primary target (192.168.20.30)
# ============================================================
# The packages below used to be installed at RUNTIME by the *_suta.sh scripts via
# apt-get/pip. Since the targets run on `internal: true` networks (no internet —
# non-negotiable containment), runtime apt/pip fails ("Temporary failure
# resolving deb.debian.org") and stalls the services' bring-up. We moved all the
# installation to BUILD-TIME (where there is internet), per the design decision
# recorded in LAB.md ("pre-satisfy egress dependencies offline"). At runtime the
# scripts only configure services/users and start them.
# ============================================================
FROM nginx:trixie

ENV DEBIAN_FRONTEND=noninteractive

# Base packages + the superset required by ALL *_suta.sh scripts of the 8 cases.
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
COPY apt41_dust_suta.sh /apt41_dust_suta.sh
COPY c0010_suta.sh /c0010_suta.sh
COPY c0026_suta.sh /c0026_suta.sh
COPY costaricto_suta.sh /costaricto_suta.sh
COPY operation_midnighteclipse_suta.sh /operation_midnighteclipse_suta.sh
COPY outer_space_suta.sh /outer_space_suta.sh
COPY salesforce_data_exfiltration_suta.sh /salesforce_data_exfiltration_suta.sh
COPY shadowray_suta.sh /shadowray_suta.sh

# Normalize line endings (strip CRLF from Windows-cloned repo) and make executable
RUN sed -i 's/\r$//' /*_suta.sh && chmod +x /*_suta.sh

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
# Normalize line endings (strip CRLF from Windows-cloned repo) and make executable
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]