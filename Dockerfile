FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py token_store.py db.py .
COPY static/ ./static/
COPY scripts/ ./scripts/
COPY change_password.py manage_tokens.py ./

# Не запускать приложение от root; том data должен быть доступен UID 10001
RUN useradd --system --uid 10001 --create-home albion \
    && mkdir -p /app/data \
    && chown -R albion:albion /app
USER albion

# Данные (uploads + json-базы + auth + tokens) живут в /app/data — монтируйте томом
ENV ALBION_DATA_DIR=/app/data \
    ALBION_HOST=0.0.0.0 \
    ALBION_PORT=5000
# Set ALBION_BASE_URL to the public HTTPS origin at deployment time.

EXPOSE 5000

CMD ["python", "server.py"]
