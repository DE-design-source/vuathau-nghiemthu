# docker build -t nghiemthu nghiem-thu && docker run -p 8000:8000 -v nghiemthu-data:/data -e SECRET_KEY=... -e ANTHROPIC_API_KEY=... -e HTTPS=1 nghiemthu
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATA_DIR=/data
VOLUME /data
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
