### Builder
FROM python:3.13.2-alpine3.21 AS builder

# Install necessary packages
RUN apk add poetry gcc python3-dev musl-dev linux-headers git 
RUN apk add libffi-dev

WORKDIR /project

# Set environment variables for Poetry
ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache

RUN touch README.md

# Add minimal files needed for poetry install
COPY pyproject.toml poetry.lock /project/

# Set Python version for Poetry and install dependencies
RUN poetry env use python3.13
RUN poetry install

### Run Image
FROM python:3.13.2-alpine3.21

# Install packages needed for runtime
RUN apk add --no-cache su-exec

# Set environment variables
ENV PATH="/project/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Rome

WORKDIR /project/

# Default configuration
ENV HSYNC_LOG_LEVEL=info

# Expose port
EXPOSE 8765

# Specify the entrypoint
ENTRYPOINT ["/project/docker/entrypoint.sh"]

# Default command
CMD ["/project/docker/run.sh"]

# Copy the virtual environment from the builder stage
COPY --from=builder /project/.venv /venv

# Project config
RUN mkdir buckets tmp

# Copy application source code
COPY docker /project/docker
COPY hsync_server /project/hsync_server
COPY config /project/config

RUN   rm -rf /project/.venv \
    && ln -s /venv /project/.venv \
    && chmod 755 /project/docker/entrypoint.sh \
    && chmod 755 /project/docker/run.sh \
    && /bin/true

# docker build -t hsync:0.1.0 .
# docker run --network="host" -e HSYNC_API_KEY="development" -e HSYNC_LOG_LEVEL="debug" hsync:0.1.0
# docker run --rm -it --entrypoint=/bin/sh hsync:0.1.0