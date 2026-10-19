FROM denoland/deno:bin-2.9.7 AS deno
FROM python:3.11-slim
COPY --from=deno /deno /usr/local/bin/deno

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 FLASK_APP=app:create_app
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app app \
    && chown 1000:1000 /app

COPY --chown=1000:1000 requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=1000:1000 . .
USER 1000:1000
EXPOSE 8005
ENTRYPOINT ["/app/scripts/start-web.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8005", "--workers", "2", "--timeout", "120", "app:create_app()"]
