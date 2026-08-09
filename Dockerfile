FROM python:3.12-alpine

WORKDIR /opt/backup

COPY engine.py server.py ./
COPY static/ static/

RUN adduser -D -u 65534 backup

USER 65534

VOLUME ["/config", "/backups"]

EXPOSE 8080

ENTRYPOINT ["python3", "server.py"]
CMD ["--config", "/config/autobrain-backup.json", "--backups", "/backups", "--port", "8080"]
