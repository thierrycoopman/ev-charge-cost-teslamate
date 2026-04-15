FROM python:3.12-slim

# Install cron (that's all we need beyond Python)
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer — only rebuilds when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy scripts
COPY scripts/ ./scripts/

# Crontab (installed system-wide so cron picks it up)
COPY crontab /etc/cron.d/tesla-sync
RUN chmod 0644 /etc/cron.d/tesla-sync && crontab /etc/cron.d/tesla-sync

# Runtime directories
RUN mkdir -p /app/logs /app/data

# Entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Tesla token lives here — mounted as a named volume so it survives rebuilds
ENV TESLA_TOKEN_FILE=/app/data/tesla_tokens.json

VOLUME ["/app/data", "/app/logs"]

ENTRYPOINT ["/entrypoint.sh"]
