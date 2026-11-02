# SpotifyForge's `history sync` (the field notes' Heard section) and its weekly playlist maintenance,
# one run at a time. Built and run from compose.yml beside this file; see docs/HOSTING.md, "The live VPS".
FROM python:3.11-slim

# A headless Linux box has no OS keyring, so the token goes to a keyrings.alt file in /login,
# readable only by this job's user. No SpotifyForge code changes for that: keyring reads the backend
# from the environment. HOME only has to exist: this uid has no passwd entry.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    SPOTIFYFORGE_DATA_DIR=/data \
    XDG_DATA_HOME=/login \
    PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring \
    HOME=/tmp \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# A pinned commit: bump it to take a newer SpotifyForge, then rebuild.
ARG SPOTIFYFORGE_COMMIT=ba5933cd74e2fc9663b229e9303cc502ca20549a
RUN pip install uv \
    && git clone --quiet https://github.com/gr8monk3ys/spotify.git /app \
    && git -C /app checkout --quiet "$SPOTIFYFORGE_COMMIT" \
    && cd /app \
    && uv sync --quiet --frozen --no-dev \
    && uv pip install --quiet --python .venv/bin/python keyrings.alt==5.0.2

COPY weekly.sh /weekly.sh

WORKDIR /app
USER 10002:10002
CMD ["spotifyforge", "history", "sync"]
