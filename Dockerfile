# astrolift-sample-api — one image, two workloads (web + worker).
# The manifest's container `command` selects the mode:
#   command=["python","entrypoint.py","web"]
#   command=["python","entrypoint.py","worker"]
FROM python:3.12-slim

# psycopg2-binary ships its own libpq, so no build deps are needed; we
# keep the image slim and add only curl for the image-level HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# GIT_SHA is stamped at build time by the GitHub Actions workflow and
# surfaces in /debug + the startup banner as the running version.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    LOG_LEVEL=info

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY worker/ ./worker/
COPY entrypoint.py ./

# Drop root for runtime.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8080

# Image-level healthcheck mirrors the manifest http probe on /health.
# Only meaningful for the web workload; the worker exits non-2xx here
# harmlessly (its liveness is the process itself / k8s exec probe).
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Default to the web workload; the worker overrides via manifest command.
CMD ["python", "entrypoint.py", "web"]
