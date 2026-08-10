# ── Stage 1: Build dependencies on Alpine ────────────────────────────────────
FROM python:3.11-alpine AS builder

WORKDIR /app

RUN apk add --no-cache gcc musl-dev libffi-dev openssl-dev rust cargo

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Final lightweight image (~16MB) ──────────────────────────────────
FROM python:3.11-alpine

WORKDIR /app

# Copy compiled Python packages from builder (no build tools left in final image)
COPY --from=builder /install /usr/local

# Copy application code
COPY templates templates
COPY static static
COPY integrations integrations
COPY services services
COPY *.py .

EXPOSE 5000

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 4 --timeout 120 app:app"]



