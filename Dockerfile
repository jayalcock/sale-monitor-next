FROM python:3.11-slim

WORKDIR /app

# System deps (optional: CA certificates, curl for debugging)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

ENV PYTHONPATH=/app/src

CMD ["python", "-m", "sale_monitor.cli.main", "--products-csv", "/app/data/products.csv", "--state-file", "/app/data/state.json"]