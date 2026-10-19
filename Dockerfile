FROM denoland/deno:bin-2.9.7 AS deno
FROM node:22-bookworm-slim AS claude
RUN npm install --global @anthropic-ai/claude-code@2.1.303 \
    && test "$(claude --version)" = "2.1.303 (Claude Code)"

FROM python:3.11-slim
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=claude /usr/local/bin/node /usr/local/bin/node
COPY --from=claude /usr/local/lib/node_modules/@anthropic-ai/claude-code /usr/local/lib/node_modules/@anthropic-ai/claude-code

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 FLASK_APP=app:create_app DISABLE_UPDATES=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app app \
    && ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude \
    && chown 1000:1000 /app

COPY --chown=1000:1000 requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=1000:1000 . .
USER 1000:1000
RUN test "$(claude --version)" = "2.1.303 (Claude Code)"
EXPOSE 8005
ENTRYPOINT ["/app/scripts/start-web.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8005", "--workers", "2", "--timeout", "120", "app:create_app()"]
