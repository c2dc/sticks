FROM nginx:trixie

RUN apt-get update && \
    apt-get install -y iputils-ping procps lsof net-tools vim iproute2 php-fpm php-mysql php-curl php-gd php-mbstring php-xml php-zip git wget curl  && \
    rm -rf /var/lib/apt/lists/*

CMD php-fpm8.4 -D && nginx -g "daemon off;"

COPY apt41_dust_suta.sh /apt41_dust_suta.sh
RUN chmod +x /apt41_dust_suta.sh
CMD ["/apt41_dust_suta.sh"]

COPY c0010_suta.sh /c0010_suta.sh
RUN chmod +x /c0010_suta.sh
CMD ["/c0010_suta.sh"]

COPY c0026_suta.sh /c0026_suta.sh
RUN chmod +x /c0026_suta.sh
CMD ["/c0026_suta.sh"]

COPY costaricto_suta.sh /costaricto_suta.sh
RUN chmod +x /costaricto_suta.sh
CMD ["/costaricto_suta.sh"]

COPY operation_midnighteclipse_suta.sh /operation_midnighteclipse_suta.sh
RUN chmod +x /operation_midnighteclipse_suta.sh
CMD ["/operation_midnighteclipse_suta.sh"]

COPY outer_space_suta.sh /outer_space_suta.sh
RUN chmod +x /outer_space_suta.sh
CMD ["/outer_space_suta.sh"]

COPY salesforce_data_exfiltration_suta.sh /salesforce_data_exfiltration_suta.sh
RUN chmod +x /salesforce_data_exfiltration_suta.sh
CMD ["/salesforce_data_exfiltration_suta.sh"]

COPY shadowray_suta.sh /shadowray_suta.sh
RUN chmod +x /shadowray_suta.sh
CMD ["/shadowray_suta.sh"]

