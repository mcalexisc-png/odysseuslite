FROM python:3.12-slim-bookworm

# System deps. tmux is required by Cookbook for background downloads/serves.
# openssh-client is required for Cookbook remote server tests, setup, probes,
# downloads, and serves from Docker installs.
# git/cmake are required when Cookbook builds llama.cpp on first llama.cpp
# launch inside Docker.
# nodejs/npm provide npx for the optional built-in Browser MCP server.
# gosu lets the entrypoint drop privileges cleanly so signals still reach
# uvicorn directly (no extra shell layer like `su`/`sudo` would add).
# bubblewrap (bwrap) is the agent-shell sandbox launcher (see THREAT_MODEL.md);
# without it the sandbox falls back to a weaker workspace jail. It's tiny, so
# the lite image ships it to keep the agent shell safe by default.
#
# Deliberately NOT pinned to a snapshot.debian.org date: a fixed snapshot's
# Release file expires after ~7-9 days, so any clone built after that window
# would fail `apt-get update` with "Release file ... is expired" — a public
# repo gets built at unpredictable future times, so a dated pin is the wrong
# tool here. The `python:3.12-slim-bookworm` tag already pins the OS major
# version; that's the right level of reproducibility for this build, and
# tracking Debian's live bookworm archive keeps the build working indefinitely.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    curl \
    git \
    nodejs \
    npm \
    tmux \
    openssh-client \
    gosu \
    bubblewrap \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Odysseus Lite installs the TRIMMED requirements (no chromadb/fastembed/onnx);
# the heavy upstream requirements.txt is intentionally NOT used here so the
# default lite image stays small and CPU-only. MEMORY_BACKEND=hybrid adds
# sqlite-vec + model2vec (already in requirements.lite.txt, inert by default).
# Optional AGPL extras (PyMuPDF, etc.) remain opt-in via requirements-optional.txt.
ARG INSTALL_OPTIONAL=false
COPY requirements.lite.txt requirements-optional.txt ./
RUN pip install --no-cache-dir -r requirements.lite.txt \
    && if [ "$INSTALL_OPTIONAL" = "true" ]; then pip install --no-cache-dir -r requirements-optional.txt; fi

# Copy app code
COPY . .

# Create data directory (mount a volume here for persistence)
RUN mkdir -p data logs services/cache/search

# Entrypoint that drops to PUID/PGID (default 1000:1000) and repairs
# ownership on the bind-mounted /app/data and /app/logs. Without this,
# the container runs as root and writes root-owned files into host
# bind mounts — any later non-root run (or a host user trying to
# update them) silently fails on EPERM, breaking skill extraction,
# prefs persistence, mail attachments, etc.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 7000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7000"]
