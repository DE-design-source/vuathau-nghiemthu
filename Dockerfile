# Xem docker-compose.yml để chạy trên máy chủ dùng chung.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd -r -u 10001 app && mkdir /data && chown app /data
COPY . .
USER app
ENV DATA_DIR=/data
VOLUME /data
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
