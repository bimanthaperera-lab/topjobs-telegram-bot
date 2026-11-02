FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATABASE_PATH=/data/jobbot.sqlite3
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jobbot ./jobbot

# The bot runs as a non-root user. It must be able to write the SQLite file both
# in /data (the default) and in /app (if DATABASE_PATH is set to a relative path).
# No VOLUME here: hosts like Render ignore it; with plain Docker, mount one with -v.
RUN useradd --create-home bot && mkdir -p /data && chown bot /data /app
USER bot

CMD ["python", "-m", "jobbot"]
