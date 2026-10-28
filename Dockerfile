FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY bot ./bot
COPY data ./data
RUN uv sync --frozen --no-dev
ENV PYTHONUNBUFFERED=1
VOLUME /app/var
CMD ["uv", "run", "--no-dev", "python", "-m", "bot"]
