FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=10000

RUN groupadd --system crossposter \
    && useradd --system --gid crossposter --home-dir /app crossposter

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY certs ./certs
COPY migrations ./migrations
COPY app ./app

RUN chown -R crossposter:crossposter /app

EXPOSE 10000

USER crossposter

CMD ["sh", "-c", "exec uvicorn app.main:create_app_from_env --factory --host 0.0.0.0 --port ${PORT:-10000}"]
