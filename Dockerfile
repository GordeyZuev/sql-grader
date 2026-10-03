FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY cabinet ./cabinet
RUN groupadd --system --gid 10001 cabinet \
    && useradd --system --uid 10001 --gid cabinet --home-dir /app cabinet \
    && mkdir -p /data /backups \
    && chown cabinet:cabinet /data /backups
RUN python -m pip install --no-cache-dir uv==0.9.21 \
    && uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "cabinet.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
