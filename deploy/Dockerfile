FROM python:3.12-slim
WORKDIR /app
COPY . /app
# Starting data, copied onto the persistent disk the first time the server starts
RUN mkdir -p /app/seed && cp data/events.json data/parties.json data/pending.json watcher/sources.json /app/seed/ \
    && chmod +x deploy/entrypoint.sh
ENV PORT=8080 DB_PATH=/app/data/lta.db PYTHONUNBUFFERED=1
EXPOSE 8080
# The server runs the backup watcher and push jobs itself (no cron needed).
CMD ["deploy/entrypoint.sh"]
