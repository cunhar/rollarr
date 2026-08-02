FROM python:3.11-alpine

WORKDIR /app

# Install dependencies first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY app.py .

EXPOSE 5000

CMD ["python", "app.py"]
