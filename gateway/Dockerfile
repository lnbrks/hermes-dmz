FROM mitmproxy/mitmproxy:latest

# Extra deps for our addons (mitmproxy itself ships in the base image).
USER root
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# socat: forwards the host webhook (ingress) to the Hermes container, so the
# gateway is the single bridge in both directions.
RUN apt-get update \
    && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/*

COPY addons.py /app/addons.py
COPY policy /app/policy
COPY config /app/config
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# The mitmproxy CA and the audit log live under the confdir, which we mount
# as a named volume so the CA survives restarts and can be shared with the
# Hermes container. Make it writable by the unprivileged mitmproxy user.
RUN mkdir -p /home/mitmproxy/.mitmproxy \
    && chown -R mitmproxy:mitmproxy /home/mitmproxy/.mitmproxy /app

USER mitmproxy
WORKDIR /app
ENV GATEWAY_ALLOWLIST=/app/config/allowlist.yaml \
    GOOGLE_REFRESH_CREDENTIALS_FILE=/run/secrets/gws-creds.json \
    GATEWAY_AUDIT_LOG=/home/mitmproxy/.mitmproxy/audit.jsonl

EXPOSE 8080 8644

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["-s", "/app/addons.py", \
     "--mode", "regular", \
     "--listen-host", "0.0.0.0", \
     "--listen-port", "8080", \
     "--set", "confdir=/home/mitmproxy/.mitmproxy", \
     "--set", "block_global=false", \
     "--set", "stream_large_bodies=10m", \
     "--showhost", \
     "--flow-detail", "0"]
