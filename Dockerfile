FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY dashboard_v2.py dashboard_v2.html ./
CMD ["sh","-c","gunicorn -w 2 -b 0.0.0.0:${PORT:-8080} --timeout 120 dashboard_v2:app"]
