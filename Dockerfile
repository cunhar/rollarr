FROM python:3.11-slim

WORKDIR /app

# Install dependencies first to leverage Docker layer caching.
# slim uses glibc (manylinux wheels), making cryptography & paramiko pip installs instant.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY templates templates
COPY static static
COPY integrations integrations
COPY services services
COPY *.py .

EXPOSE 5000

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 4 --timeout 120 app:app"]


