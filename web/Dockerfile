# VallShop публичный сайт — отдельный образ (не зависит от контейнера бота).
# Переиспользует общий Python-пакет bot/ (логика, модели), подключается к общей БД.
FROM python:3.11-slim

WORKDIR /app

COPY web/requirements.txt /app/web/requirements.txt
RUN pip install --no-cache-dir -r /app/web/requirements.txt

# Общий код с ботом (бизнес-логика, модели) + код сайта.
COPY bot /app/bot
COPY web /app/web

ENV PYTHONPATH=/app
EXPOSE 8090

CMD ["uvicorn", "web.app.main:app", "--host", "0.0.0.0", "--port", "8090"]
