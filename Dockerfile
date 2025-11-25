FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Certificates for HTTPS requests
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir supervisor

# App source
COPY src ./src
COPY supervisord.conf /app/supervisord.conf
ENV PYTHONPATH=/app/src

# Data dir (will be mounted)
RUN mkdir -p /app/data

EXPOSE 5000
CMD ["/usr/local/bin/supervisord", "-c", "/app/supervisord.conf"]