# B2B Email Finder: web app + command line in one image.
#
#   docker compose up            -> web app on http://localhost:8501
#   docker compose run --rm app b2b-email-finder search --area "Köln" --out results/koeln
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    B2B_DEMO_DIR=/app/demo

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[ui,sources]"

# Example result files shown on first start (copied into the data folder if it is empty).
COPY demo ./demo

# Run as a normal user. /data holds results/ and .cache/ and is meant to be a volume.
RUN useradd --create-home app && mkdir -p /data && chown app:app /data
USER app
WORKDIR /data

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"

CMD ["b2b-email-finder", "ui", "--results-dir", "/data/results", \
     "--server.headless", "true", "--server.address", "0.0.0.0"]
