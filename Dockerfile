FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/app/data/bot.sqlite3

WORKDIR /app

RUN apk add --no-cache ca-certificates tzdata \
    && addgroup -S app \
    && adduser -S app -G app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py pyproject.toml ./
COPY src ./src

RUN mkdir -p /app/data \
    && chown -R app:app /app

USER app

CMD ["python", "bot.py"]
