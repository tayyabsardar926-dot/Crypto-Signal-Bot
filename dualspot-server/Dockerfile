FROM python:3.12-slim
WORKDIR /app
COPY dualspot-server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY dualspot-server/ .
CMD ["sh","-c","gunicorn -b 0.0.0.0:${PORT:-8080} --workers 1 --threads 8 --timeout 120 app:app"]
