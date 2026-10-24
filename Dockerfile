FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Optional: Scrapling browsers (only if SCRAPLING_BROWSERS=1 at build time)
ARG SCRAPLING_BROWSERS=0
RUN if [ "$SCRAPLING_BROWSERS" = "1" ]; then scrapling install || true; fi

COPY . .

# Non-root user
RUN useradd -m collector && chown -R collector /app
USER collector

CMD ["python", "-m", "collector", "run", "--all"]
