FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# The vault lives on the host: mount it here (see docker-compose.yml).
ENV VAULT_PATH=/vault HOST=0.0.0.0 PORT=8700
VOLUME /vault
EXPOSE 8700
CMD ["/app/.venv/bin/rpg-llm"]
