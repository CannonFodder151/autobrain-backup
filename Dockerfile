FROM python:3.12-alpine

RUN apk add --no-cache curl su-exec

WORKDIR /opt/backup

COPY engine.py server.py ./
COPY static/ static/
COPY entrypoint.sh ./

RUN chmod +x entrypoint.sh \
    && adduser -D -u 10001 -g '' backup

# Root needed at build/runtime so entrypoint can chown volumes before dropping
# privileges. The entrypoint script handles the privilege drop to 10001.

VOLUME ["/config", "/backups"]

HEALTHCHECK --interval=30s --timeout=5s CMD curl -f http://localhost:8080/healthz || exit 1

ENTRYPOINT ["./entrypoint.sh"]
CMD ["--config", "/config/autobrain-backup.json", "--backups", "/backups", "--port", "8080"]
