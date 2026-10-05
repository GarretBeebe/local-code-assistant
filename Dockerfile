FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /bin/uv
WORKDIR /app
# Install exactly what uv.lock pins, so the image runs the versions the tests ran.
# The app itself is not installed: uvicorn imports proxy/ and settings.py from the working dir.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-cache
COPY . .
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd -r -s /bin/false appuser
USER appuser
CMD exec uvicorn proxy.server:app --host 0.0.0.0 --port ${PROXY_PORT:-8080}
