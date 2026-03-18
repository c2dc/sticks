FROM nginx:trixie

RUN apt-get update && \
    apt-get install -y iputils-ping procps lsof net-tools vim iproute2 nginx php-fpm php-mysql php-curl php-gd php-mbstring php-xml php-zip git wget curl net-tools bind9-dnsutils

# Copy campaign setup scripts
COPY apt41_dust_sutb.sh /apt41_dust_sutb.sh
COPY c0010_sutb.sh /c0010_sutb.sh
COPY c0026_sutb.sh /c0026_sutb.sh
COPY costaricto_sutb.sh /costaricto_sutb.sh
COPY operation_midnighteclipse_sutb.sh /operation_midnighteclipse_sutb.sh
COPY outer_space_sutb.sh /outer_space_sutb.sh
COPY salesforce_data_exfiltration_sutb.sh /salesforce_data_exfiltration_sutb.sh
COPY shadowray_sutb.sh /shadowray_sutb.sh

RUN chmod +x /*_sutb.sh

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
