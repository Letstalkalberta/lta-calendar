FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir google-auth requests && apt-get update && apt-get install -y --no-install-recommends cron && rm -rf /var/lib/apt/lists/*
COPY . /app
# watcher every 6 hours, push job every 15 minutes, page rebuild after each watch
RUN printf '15 */6 * * * cd /app && python3 watcher/watch.py --stale-hours 24 >> /var/log/watch.log 2>&1 && python3 build_page.py >> /var/log/watch.log 2>&1\n*/15 * * * * cd /app && python3 server/push.py >> /var/log/push.log 2>&1\n' > /etc/cron.d/lta && chmod 0644 /etc/cron.d/lta && crontab /etc/cron.d/lta
ENV PORT=8080 DB_PATH=/app/data/lta.db
EXPOSE 8080
# data/ should be a mounted volume so rides, devices and approved events survive redeploys
CMD printenv | grep -E '^(ANTHROPIC|LTA_|FCM_|GOOGLE_|DB_PATH|PUBLIC_BASE|CALENDAR_PAGE)' >> /etc/environment; cron && python3 server/server.py
