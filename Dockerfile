# Dockerfile
FROM python:3.11-slim

# حزم مساعدة لشهادات/SSL
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py /app/

# Render/ Railway يمرّر PORT
ENV PYTHONUNBUFFERED=1
CMD ["python", "server.py"]
