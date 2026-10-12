# Front Desk, the Discord bot, as a small always-on container. See docs/HOSTING.md.
#   docker compose up -d --build
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    TZ=America/Los_Angeles

# Welcome cards shape Arabic and Hebrew names with Raqm (bot/logic/cards.py). Pillow's wheels
# bundle Raqm and HarfBuzz but load FriBiDi from the system at run time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libfribidi0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pillow is imported by the card and rank-card code but is not listed in requirements.txt.
COPY bot/requirements.txt bot/requirements.txt
RUN pip install -r bot/requirements.txt "pillow>=10"

COPY bot/ bot/
COPY server/ server/

# - The code stays owned by root and read-only for the bot: only bot/data is writable.
#   (That is also what makes the nightly backup fall back to bot/data/backups on Linux.)
# - bot/data is a volume, which hides the question/poll banks shipped in the repo, so keep a
#   copy in /app/seed for run_container.sh to refresh on every start.
# - sed strips CR in case the repo was checked out on Windows with CRLF line endings.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin frontdesk \
    && mkdir -p /app/seed /app/bot/data \
    && cp /app/bot/data/qotd.txt /app/bot/data/polls.txt /app/seed/ \
    && sed -i 's/\r$//' /app/bot/run_container.sh \
    && chmod 755 /app/bot/run_container.sh \
    && chown -R frontdesk:frontdesk /app/bot/data

USER frontdesk
WORKDIR /app/bot
VOLUME /app/bot/data

# Ctrl+C-style stop: discord.py closes the gateway and the database cleanly.
STOPSIGNAL SIGINT

# The bot rewrites bot/data/heartbeat every minute while connected. Older than 3 minutes
# (or missing) = unhealthy. The first 3 minutes after a start don't count.
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=2 \
    CMD ["/app/bot/run_container.sh", "health"]

ENTRYPOINT ["/app/bot/run_container.sh"]
CMD ["bot"]
