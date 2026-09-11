FROM python:3.12-alpine

WORKDIR /opt/backup

COPY engine.py server.py ./
COPY static/ static/

RUN adduser -D -u 10001 -g '' backup

USER 65534

VOLUME ["/config", "/backups"]

HEALTHCHECK --interval=30s --timeout=5s CMD curl -f http://localhost:8080/healthz || exit 1

ENTRYPOINT ["python3", "server.py"]
CMD ["--config", "/config/autobrain-backup.json", "--backups", "/backups", "--port", "8080"]
