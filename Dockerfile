# One image, two processes: the portal (the default command) and the bot
# (`python bot.py`). compose.yaml runs both; see README.md, "Deployment and CI/CD".
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/rms_bot.sqlite3 \
    STORAGE_ROOT=/data/sites

WORKDIR /app

# Dependencies first, so a code-only change reuses this layer. httpx2 is only
# for the post-deploy smoke check (tools/smoke_portal.py drives the app with
# Starlette's TestClient).
COPY requirements.txt .
RUN pip install -r requirements.txt httpx2

COPY . .

# Not root. Everything that lasts (the database, downloaded attachments) is on
# the /data volume, owned by this user.
RUN useradd --uid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin rms \
    && mkdir -p /data/sites && chown -R rms:rms /data
USER rms
VOLUME ["/data"]
EXPOSE 8000

# --forwarded-allow-ips '*': the client's address comes from the proxy in
# front (Caddy, or an ingress), which the sign-in throttle needs. Safe only
# because nothing else can reach this port: compose publishes it on loopback.
CMD ["uvicorn", "portal.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
