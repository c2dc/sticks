FROM nginx:trixie

RUN apt-get update && \
    apt-get install -y iputils-ping procps lsof net-tools vim iproute2 nginx php-fpm php-mysql php-curl php-gd php-mbstring php-xml php-zip git wget curl  && \
    rm -rf /var/lib/apt/lists/*

# Copy campaign setup scripts
COPY apt41_dust_suta.sh /apt41_dust_suta.sh
COPY c0010_suta.sh /c0010_suta.sh
COPY c0026_suta.sh /c0026_suta.sh
COPY costaricto_suta.sh /costaricto_suta.sh
COPY operation_midnighteclipse_suta.sh /operation_midnighteclipse_suta.sh
COPY outer_space_suta.sh /outer_space_suta.sh
COPY salesforce_data_exfiltration_suta.sh /salesforce_data_exfiltration_suta.sh
COPY shadowray_suta.sh /shadowray_suta.sh

RUN chmod +x /*_suta.sh

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
