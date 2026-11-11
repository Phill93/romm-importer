FROM python:3.12-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH"
WORKDIR /build
RUN python -m venv "$VIRTUAL_ENV" && pip install --no-cache-dir poetry==2.4.3
COPY pyproject.toml poetry.lock README.md ./
RUN poetry config virtualenvs.create false && poetry install --only main --no-root
COPY src ./src
RUN poetry build --format wheel

FROM node:22-bookworm-slim AS igir
RUN npm install --global igir@5.7.2

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install --no-install-recommends -y mame-tools p7zip-full \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 importer && useradd --uid 10001 --gid importer --create-home --home-dir /app importer
COPY --from=igir /usr/local/bin/node /usr/local/bin/node
COPY --from=igir /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/igir/dist/index.js /usr/local/bin/igir
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/dist/*.whl /tmp/
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-deps /tmp/*.whl && rm /tmp/*.whl
COPY --chown=importer:importer src/romm_importer/templates ./src/romm_importer/templates
COPY --chown=importer:importer src/romm_importer/static ./src/romm_importer/static
USER importer
EXPOSE 30062
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:30062/healthz', timeout=3)" || exit 1
CMD ["uvicorn", "romm_importer.main:app", "--host", "0.0.0.0", "--port", "30062"]
