FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .
COPY config ./config
# state lives in a volume; the unprivileged user must own it
RUN useradd --create-home bot && mkdir -p /app/state && chown -R bot /app/state
USER bot
CMD ["cryptowatcher", "run"]
