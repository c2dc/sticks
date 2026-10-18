# ============================================================
# KALI ATTACKER — SLIM BUILD
# ============================================================
# The full Kali metapackages (kali-linux-headless) plus
# metasploit-framework pull ~2.4 GB from the kali-rolling mirror and were the
# source of the HTTP 403 failures during the lab build: the rolling mirror
# intermittently rejects a handful of the hundreds of packages those
# metapackages drag in.
#
# None of the curated campaigns (APT41-DUST, C0010, C0026, CostaRicto,
# MidnightEclipse, Outer Space, Salesforce, ShadowRay) use metasploit or
# sqlmap. Auditing every curated command, the attacker container only needs:
#   curl, wget            — HTTP ingress / exfil
#   nmap                  — scanning / discovery
#   sshpass, openssh      — ssh/scp into the internal targets
#   netcat (nc)           — raw TCP (CostaRicto)
#   dnsutils (dig/host)   — DNS discovery (C0010, C0026, CostaRicto)
#   zip                   — archive collection (C0026)
#   psmisc (fuser)        — port cleanup (CostaRicto tunnel)
#   python3, coreutils    — payload decode/run (base64/python3)
#   mailutils             — email techniques (Salesforce)
#   iputils-ping          — connectivity checks
# Installing only these keeps the image small and sidesteps the flaky
# metapackage downloads entirely, so the build is reliable.
# ============================================================
FROM kalilinux/kali-rolling

ENV DEBIAN_FRONTEND=noninteractive
ENV SERVER=http://192.168.10.10:8888

# Retry transient mirror failures a few times instead of aborting the whole
# build on the first hiccup (the 403s were intermittent, not permanent).
RUN printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\n' \
        > /etc/apt/apt.conf.d/80-retries

# Install ONLY the tools the curated commands actually invoke.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        nmap \
        curl \
        wget \
        sshpass \
        openssh-client \
        netcat-traditional \
        dnsutils \
        zip \
        psmisc \
        iputils-ping \
        python3 \
        coreutils \
        mailutils \
        ca-certificates \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Run agent when container starts
# Download sandcat agent
CMD ["sh", "-c", "curl -s -X POST -H 'file:sandcat.go' -H 'platform:linux' http://caldera:8888/file/download -o /usr/local/bin/sandcat && chmod +x /usr/local/bin/sandcat && exec /usr/local/bin/sandcat -server http://caldera:8888 -group red -v"]