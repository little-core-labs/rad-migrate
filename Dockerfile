# rad-migrate — YouTube to Rad TV.
#
# Ships the whole toolchain, which is most of the setup burden: Python, Node (required
# for HD — YouTube's stream URLs are behind a JS challenge), and ffmpeg.
#
#   docker run --rm -e RAD_API_KEY -v "$PWD/work:/work" \
#     ghcr.io/little-core-labs/rad-migrate run @yourchannel --workdir /work
#
# Browser cookie extraction cannot work in a container. If you need cookies, mount a
# cookies.txt and pass --cookies-file /work/cookies.txt.

FROM python:3.15-slim AS base

# Node 22 from nodesource; ffmpeg from Debian.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg ffmpeg; \
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    apt-get purge -y --auto-remove gnupg; \
    rm -rf /var/lib/apt/lists/*

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv build --wheel --out-dir /dist

FROM base AS runtime
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

# Downloads and resume state belong on a mounted volume, not in the image layer.
ENV RAD_MIGRATE_WORKDIR=/work \
    RAD_MIGRATE_DATA_DIR=/work/.rad-migrate \
    PYTHONUNBUFFERED=1
VOLUME ["/work"]
WORKDIR /work

# Unprivileged: nothing here needs root, and the workdir is a mount.
RUN useradd --create-home --uid 10001 rad-migrate \
    && mkdir -p /work && chown -R rad-migrate:rad-migrate /work
USER rad-migrate

ENTRYPOINT ["rad-migrate"]
CMD ["--help"]
