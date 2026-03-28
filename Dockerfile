FROM python:3.11-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies in a separate layer for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ src/
COPY templates/ templates/
COPY docs/FEEDS.md docs/FEEDS.md

# Create data directory for SQLite and output
RUN mkdir -p data

# Run as non-root
RUN useradd -m -s /bin/bash app && chown -R app:app /app
USER app

# Default: run the pipeline
CMD ["python", "-m", "src.main"]
