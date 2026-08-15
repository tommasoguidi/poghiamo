# ---------- Stage 0: CSS build ----------
FROM node:20-slim AS css-builder

WORKDIR /css
COPY package.json package-lock.json tailwind.config.js ./
COPY src/poghiamo/webapp/static/input.css src/poghiamo/webapp/static/input.css
COPY src/poghiamo/webapp/templates/ src/poghiamo/webapp/templates/

RUN npm ci \
    && npx tailwindcss \
       -i src/poghiamo/webapp/static/input.css \
       -o /out/tailwind.css \
       --minify

# ---------- Stage 1: Python build (uv, lockfile-faithful) ----------
# Same distro (trixie) as the runtime stage so the venv relocates cleanly.
FROM ghcr.io/astral-sh/uv:0.9-python3.12-trixie-slim AS builder

WORKDIR /build
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1

# Dependency layer first (cached until pyproject/uv.lock change).
# --locked (not --frozen): the build FAILS if uv.lock is stale vs pyproject.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Then the project itself, non-editable so the venv is self-contained
COPY README.md ./
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim-trixie

COPY --from=builder /opt/venv /opt/venv
# Built Tailwind CSS into the installed package's static directory
COPY --from=css-builder /out/tailwind.css /opt/venv/lib/python3.12/site-packages/poghiamo/webapp/static/tailwind.css

ENV PATH="/opt/venv/bin:$PATH" \
    # Production defaults: the bare image must be runnable without compose.
    DATABASE_URL=sqlite:////app/data/poghiamo.db \
    UVICORN_HOST=0.0.0.0 \
    UVICORN_RELOAD=false

RUN useradd --create-home appuser \
    && mkdir -p /app/data /app/backups \
    && chown appuser:appuser /app/data /app/backups

USER appuser
WORKDIR /app

CMD ["poghiamo-web"]
