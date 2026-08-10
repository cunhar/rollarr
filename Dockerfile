FROM python:3.11-alpine

WORKDIR /app

# openssl is needed to generate the self-signed TLS cert on first run
RUN apk add --no-cache openssl

# Install dependencies first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY templates templates
COPY static static
COPY integrations integrations
COPY services services
COPY *.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 5000

CMD ["./entrypoint.sh"]
