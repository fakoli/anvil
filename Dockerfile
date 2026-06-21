# anvil-mcp — FastMCP (stdio) server image for the Docker MCP catalog.
#
# Packages the engine at bin/src/anvil and starts the stdio MCP server.
# Project state lives in .anvil/ resolved from ANVIL_ROOT, so the
# host project is bind-mounted at runtime — the image itself stays stateless.
#
# Build (from the repo root):
#   docker build -t anvil-mcp .
#
# Smoke test (must print and exit 0, never block on stdio):
#   docker run --rm anvil-mcp --help
#
# Run against a host project:
#   docker run --rm -i \
#     -v "$PWD:/project" -e ANVIL_ROOT=/project \
#     anvil-mcp
#
# uv's distroless-friendly image ships uv + a managed CPython. We pin a digest-
# free tag here for readability; the catalog manifest records the pinned digest.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Never write .pyc, fail fast on uv resolution drift, keep stdio unbuffered so
# the MCP client sees handshake bytes immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

# --- Dependency layer -------------------------------------------------------
# Copy only the lockfile + manifest first so the (slow) dependency install layer
# is cached across source-only edits. The engine's pyproject lives under bin/.
# pyproject's `readme = "README.md"` is build-root-local, so bin/README.md must
# sit next to pyproject.toml for the build backend to resolve metadata.
COPY bin/pyproject.toml bin/uv.lock bin/README.md /app/bin/

# Install runtime dependencies into a project-local .venv without the source yet
# (--no-install-project). Frozen = honor uv.lock exactly; no provider extras by
# default (the MCP surface needs none — LLM extras stay opt-in like the host).
RUN cd /app/bin && uv sync --frozen --no-dev --no-install-project

# --- Source layer -----------------------------------------------------------
COPY bin/src /app/bin/src

# Now install the project itself against the cached dependency layer.
RUN cd /app/bin && uv sync --frozen --no-dev

# Resolve project state from a bind-mounted directory by default. Callers can
# override with `-e ANVIL_ROOT=...`; the server falls back to cwd when
# unset, so this is a convenience default, not a hard requirement.
ENV ANVIL_ROOT=/project
VOLUME ["/project"]

# Activate the project's uv-managed virtualenv by PATH so the runtime entry
# point invokes the *installed* interpreter directly — NOT `uv run`. `uv run`
# would attempt a re-sync/editable-rebuild at startup, which fails for the
# non-root user against the root-owned .venv. Running the venv python directly
# sidesteps that entirely and is faster (no resolution on every container start).
ENV VIRTUAL_ENV=/app/bin/.venv
ENV PATH="/app/bin/.venv/bin:${PATH}"

# Run as a non-root user. State writes happen under the bind-mounted /project,
# whose ownership is controlled by the host mount, so no chown is needed here.
RUN useradd --create-home --uid 10001 fakoli
USER fakoli

# ENTRYPOINT is the server module; CMD is empty so `docker run ... --help`
# appends --help as an argument the entry point handles (print + exit 0).
ENTRYPOINT ["python", "-m", "anvil.mcp_server"]
CMD []
