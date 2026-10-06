FROM python:3.11-slim

WORKDIR /app

# Cài đặt curl để hỗ trợ container healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ .

# Tạo thư mục /data cho SQLite database
RUN mkdir -p /data && chmod 777 /data

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
