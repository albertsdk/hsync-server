### Builder
FROM python:3.13.11-alpine3.22 AS builder

# Install build dependencies in a single layer
RUN apk add --no-cache \
    poetry \
    gcc \
    python3-dev \
    musl-dev \
    linux-headers \
    libffi-dev \
    git

WORKDIR /project

# Set environment variables for Poetry
ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache

# Add minimal files needed for poetry install
RUN touch README.md
COPY pyproject.toml poetry.lock /project/

# Install dependencies and clean cache in single layer
RUN poetry env use python3.13 \
    && poetry install --only main --no-root \
    && rm -rf /tmp/poetry_cache

### Run Image
FROM python:3.13.11-alpine3.22

# Install runtime packages
RUN apk add --no-cache su-exec

WORKDIR /project/

# Copy the virtual environment from the builder stage
COPY --from=builder /project/.venv /venv

# Copy application source code (ordered by change frequency)
COPY config /project/config
COPY hsync_server /project/hsync_server

# Setup directories and permissions in single layer
RUN mkdir -p buckets tmp \
    && ln -s /venv /project/.venv

# Set environment variables with defaults
# These can be overridden via --env-file or -e flags
ENV PATH="/project/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TZ=UTC \
    HSYNC_LOG_LEVEL=info \
    HSYNC_PORT=8765 \
    HSYNC_WORKERS=1 \
    HSYNC_API_KEY=""

# Expose default port (can be changed via HSYNC_PORT)
EXPOSE 8765

# Run as nobody user for security
USER nobody

# Start uvicorn server with configurable parameters
CMD ["sh", "-c", "uvicorn hsync_server.main:app --host 0.0.0.0 --port ${HSYNC_PORT} --log-config config/logging-${HSYNC_LOG_LEVEL}.yaml --log-level ${HSYNC_LOG_LEVEL} --workers ${HSYNC_WORKERS}"]