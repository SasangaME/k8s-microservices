FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir redis psycopg2-binary
COPY app/worker.py .
CMD ["python", "-u", "worker.py"]
