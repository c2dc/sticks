FROM kalilinux/kali-rolling

ENV DEBIAN_FRONTEND=noninteractive
ENV SERVER=http://172.20.0.10:8888

# Install tools
RUN apt-get update && \
    apt-get install -y \
        kali-linux-headless \
        nmap \
        sqlmap \
        iputils-ping \
        metasploit-framework \
        curl \
	git \
	sshpass \
	wget \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Run agent when container starts
# Download sandcat agent
CMD ["sh", "-c", "curl -s -X POST -H 'file:sandcat.go' -H 'platform:linux' http://caldera:8888/file/download -o /usr/local/bin/sandcat && chmod +x /usr/local/bin/sandcat && exec /usr/local/bin/sandcat -server http://caldera:8888 -group red -v"]
