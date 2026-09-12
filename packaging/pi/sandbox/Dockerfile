# anvil-pi sandbox image (M4) — a pi child that can ONLY do what the baked,
# hash-pinned allowlist says.
#
# Trust model (advisory-reviewed):
# - Build time: artifacts are copied in and pin-verified (validate + tree-hash
#   compare) — the build FAILS on any drift between allowlist pins and bytes.
# - Run time: the entrypoint re-runs validation and the launcher re-stages from
#   the baked bytes, so verification runs on every container start; nothing
#   trusts build time alone, and nothing trusts runtime alone.
# - The image supply chain itself (pi/npm/python deps, base image) is part of
#   the trusted computing base: tree pins authenticate anvil-pi's bytes, not
#   the rest of the image. CI consumes this image by digest from a trusted
#   runner build.
#
# Build (repo root):  docker build -f packaging/pi/sandbox/Dockerfile -t anvil-pi-sandbox .
# Run:                scripts/pi-sandbox-docker.sh <profile> <task-file> <workspace>
#
# Layout inside the image mirrors the repo-relative pin resolution:
#   /opt/anvil-sandbox/packaging/pi/sandbox/allowlist.json  (+ mock-llm.mjs, entrypoint.sh)
#   /opt/anvil-sandbox/packaging/pi/anvil-pi/               (pinned tree)
#   /opt/anvil-sandbox/scripts/pi-sandbox-{policy,launch}.mjs, pi-sandbox-run.sh
#
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    DEBIAN_FRONTEND=noninteractive

# node 24 (pi harness runtime) + curl for the nodesource keyring.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg git \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# pi harness, version-pinned to the version this repo was built against.
RUN npm install --global --no-audit --no-fund @earendil-works/pi-coding-agent@0.85.1

# anvil CLI: dependency layer first (cacheable), then the frozen wheel install.
COPY bin/pyproject.toml bin/uv.lock bin/README.md /app/bin/
RUN cd /app/bin && uv sync --frozen --no-dev --no-install-project
COPY bin/src /app/bin/src
COPY bin/hatch_build.py /app/bin/hatch_build.py
RUN cd /app/bin && uv sync --frozen --no-dev
ENV VIRTUAL_ENV=/app/bin/.venv
ENV PATH="/app/bin/.venv/bin:${PATH}"

# Sandbox model: state lives in the MOUNTED workspace (in-repo .anvil/), never
# in a container-internal HOME that dies with the container.
ENV ANVIL_STATE_LAYOUT=local

# Sandbox artifacts: scripts + policy + pinned extension tree (repo-relative
# layout preserved so allowlist pins resolve identically to the checkout).
COPY scripts/pi-sandbox-policy.mjs scripts/pi-sandbox-launch.mjs scripts/pi-sandbox-run.sh /opt/anvil-sandbox/scripts/
COPY packaging/pi/sandbox /opt/anvil-sandbox/packaging/pi/sandbox
COPY packaging/pi/anvil-pi /opt/anvil-sandbox/packaging/pi/anvil-pi
COPY packaging/pi/sandbox/dogfood-sequence.json /opt/anvil-sandbox/dogfood-sequence.json

# Build-time pin verification: the build FAILS if baked bytes drift from pins.
RUN node /opt/anvil-sandbox/packaging/pi/sandbox/verify-baked-pins.mjs

RUN chmod 0755 /opt/anvil-sandbox/packaging/pi/sandbox/entrypoint.sh \
    && useradd --create-home --uid 10001 sandbox \
    && mkdir -p /work /seed \
    && chown sandbox:sandbox /work /seed
USER sandbox
WORKDIR /work

# Scratch writable space (staging dirs, agent dir) on an otherwise read-only rootfs.
VOLUME ["/work"]
ENTRYPOINT ["/opt/anvil-sandbox/packaging/pi/sandbox/entrypoint.sh"]