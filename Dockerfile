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
COPY docker /project/docker
COPY hsync_server /project/hsync_server

# Setup directories and permissions in single layer
RUN mkdir -p buckets tmp \
    && ln -s /venv /project/.venv \
    && chmod 755 /project/docker/entrypoint.sh \
    && chmod 755 /project/docker/run.sh

# Set environment variables
ENV PATH="/project/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Rome \
    HSYNC_LOG_LEVEL=info

# Expose port
EXPOSE 8765

# Specify the entrypoint and default command
ENTRYPOINT ["/project/docker/entrypoint.sh"]
CMD ["/project/docker/run.sh"]

# docker build -t hsync:0.1.0 .
# docker run --network="host" -e HSYNC_API_KEY="development" -e HSYNC_LOG_LEVEL="debug" hsync:0.1.0
# docker run --rm -it --entrypoint=/bin/sh hsync:0.1.0