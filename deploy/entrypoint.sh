#!/bin/sh
# data/ is the persistent disk. Seed it on first start; never overwrite what's already there.
mkdir -p /app/data
for f in events.json parties.json pending.json sources.json; do
  [ -f "/app/data/$f" ] || cp "/app/seed/$f" "/app/data/$f"
done
exec python3 /app/server/server.py
