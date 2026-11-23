# ── Stage 1: Build dependencies on Alpine ────────────────────────────────────
FROM python:3.11-alpine AS builder

WORKDIR /app

RUN apk add --no-cache gcc g++ make curl musl-dev libffi-dev openssl-dev rust cargo

# Build official RARLAB unrar (Alpine's 7zip omits RAR/RAR5 support)
RUN curl -fsSL https://www.rarlab.com/rar/unrarsrc-7.1.3.tar.gz | tar -xz -C /tmp \
    && cd /tmp/unrar \
    && make -j$(nproc) \
    && install -m 755 unrar /usr/local/bin/unrar

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Final lightweight image ─────────────────────────────────────────
FROM python:3.11-alpine

WORKDIR /app

# Usenet post-processing tools: PAR2 repair + 7zip + official RARLAB unrar
RUN apk add --no-cache par2cmdline 7zip libstdc++

# Copy compiled Python packages and unrar binary from builder (no build tools left in final image)
COPY --from=builder /install /usr/local
COPY --from=builder /usr/local/bin/unrar /usr/local/bin/unrar

# Copy application code
COPY templates templates
COPY static static
COPY integrations integrations
COPY services services
COPY *.py .

EXPOSE 5000

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 4 --timeout 120 app:app"]
