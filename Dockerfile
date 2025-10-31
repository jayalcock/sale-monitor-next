FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Certificates for HTTPS requests
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source
COPY src ./src
ENV PYTHONPATH=/app/src

# Data dir (will be mounted)
RUN mkdir -p /app/data

CMD ["python", "-m", "sale_monitor.cli.main", "--products-csv", "/app/data/products.csv", "--state-file", "/app/data/state.json"]