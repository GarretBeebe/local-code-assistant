FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
# Stub package so hatchling can resolve the editable install without the full source tree.
# The COPY below overwrites with the real package.
RUN mkdir -p proxy && touch proxy/__init__.py && pip install --no-cache-dir -e .
COPY . .
CMD exec uvicorn proxy.server:app --host 0.0.0.0 --port ${PROXY_PORT:-8080}
