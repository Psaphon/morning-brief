# Stage 1: Builder — install Python dependencies in isolation
FROM python:3.11-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# Stage 2: Runtime — lean final image with no build tools
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install git for gh-pages deploy script
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY src/ src/
COPY templates/ templates/
COPY scripts/ scripts/
COPY docs/FEEDS.md docs/FEEDS.md

# Copy test infrastructure so tests can run inside the container
COPY tests/ tests/
COPY pyproject.toml .

# Create data directories for SQLite, output, and logs
RUN mkdir -p data/output data/logs

# Run as non-root
RUN useradd -m -s /bin/bash app && chown -R app:app /app
USER app

# Health check: dashboard.html must exist and be less than 25 hours old
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "\
import os, time; \
f = 'data/output/dashboard.html'; \
exit(0 if os.path.exists(f) and (time.time() - os.path.getmtime(f)) < 90000 else 1)"

CMD ["python", "-m", "src.main"]
