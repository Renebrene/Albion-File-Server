FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Данные (uploads + json-базы + auth + tokens) живут в /app/data — монтируйте томом
ENV ALBION_DATA_DIR=/app/data \
    ALBION_HOST=0.0.0.0 \
    ALBION_PORT=5000

EXPOSE 5000

CMD ["python", "server.py"]
