FROM debian:trixie-slim

# Install Python 3 (required by docker-systemctl-replacement) and curl for testing
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install docker-systemctl-replacement as /usr/local/bin/systemctl3.py
# and set up a dpkg diversion so that apt-installed systemd's /usr/bin/systemctl
# (and /usr/bin/systemd-tmpfiles) never overwrite our replacement.
ADD https://raw.githubusercontent.com/gdraheim/docker-systemctl-replacement/master/files/docker/systemctl3.py /usr/local/bin/systemctl3.py
RUN chmod +x /usr/local/bin/systemctl3.py && \
    dpkg-divert --add --rename --divert /usr/bin/systemctl.real /usr/bin/systemctl && \
    ln -sf /usr/local/bin/systemctl3.py /usr/bin/systemctl && \
    dpkg-divert --add --rename --divert /usr/bin/systemd-tmpfiles.real /usr/bin/systemd-tmpfiles && \
    printf '#!/bin/sh\nexit 0\n' > /usr/bin/systemd-tmpfiles && \
    chmod +x /usr/bin/systemd-tmpfiles

# Use systemctl3.py as PID 1
ENTRYPOINT ["/usr/bin/python3", "/usr/local/bin/systemctl3.py", "--init"]
