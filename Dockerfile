FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATABASE_PATH=/data/jobbot.sqlite3
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jobbot ./jobbot

RUN useradd --create-home bot && mkdir -p /data && chown bot /data
USER bot
VOLUME ["/data"]

CMD ["python", "-m", "jobbot"]
