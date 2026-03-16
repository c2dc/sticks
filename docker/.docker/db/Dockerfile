FROM nginx:trixie

RUN apt-get update && \
    apt-get install -y iputils-ping procps lsof net-tools vim iproute2 php-fpm php-mysql php-curl php-gd php-mbstring php-xml php-zip git wget curl  && \
    rm -rf /var/lib/apt/lists/*

COPY apt41_dust_sutb.sh /apt41_dust_sutb.sh
RUN chmod +x /apt41_dust_sutb.sh
CMD ["/apt41_dust_sutb.sh"]

COPY c0010_sutb.sh /c0010_sutb.sh
RUN chmod +x /c0010_sutb.sh
CMD ["/c0010_sutb.sh"]

COPY c0026_sutb.sh /c0026_sutb.sh
RUN chmod +x /c0026_sutb.sh
CMD ["/c0026_sutb.sh"]

COPY costaricto_sutb.sh /costaricto_sutb.sh
RUN chmod +x /costaricto_sutb.sh
CMD ["/costaricto_sutb.sh"]

COPY operation_midnighteclipse_sutb.sh /operation_midnighteclipse_sutb.sh
RUN chmod +x /operation_midnighteclipse_sutb.sh
CMD ["/operation_midnighteclipse_sutb.sh"]

COPY outer_space_sutb.sh /outer_space_sutb.sh
RUN chmod +x /outer_space_sutb.sh
CMD ["/outer_space_sutb.sh"]

COPY salesforce_data_exfiltration_sutb.sh /salesforce_data_exfiltration_sutb.sh
RUN chmod +x /salesforce_data_exfiltration_sutb.sh
CMD ["/salesforce_data_exfiltration_sutb.sh"]

COPY shadowray_sutb.sh /shadowray_sutb.sh
RUN chmod +x /shadowray_sutb.sh
CMD ["/shadowray_sutb.sh"]

