# Pellet burner monitor - runs on a Raspberry Pi (arm64) or any Linux PC.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BURNER_PORT=auto \
    BURNER_HTTP_PORT=8787 \
    BURNER_REMOTE_START=1 \
    BURNER_LOG_MAX_MB=50

RUN pip install --no-cache-dir pyserial==3.5 segno==1.6.1 \
 && useradd --uid 1000 --create-home --groups dialout burner

WORKDIR /app
COPY burner_responder.py burner_common.py burner_ext.py burner_features.py burner_notify.py burner_phone.py burner_sniff.py ./
COPY scada/ ./scada/
COPY docs/guide.html ./docs/
RUN mkdir -p /app/data && chown -R burner:burner /app

USER burner
EXPOSE 8787

# Settings come from the environment (docker-compose.yml, .env - see .env.example).
# BURNER_PORT "auto" = the one USB-serial adapter plugged in, found again after every unplug/replug.
# With more than one adapter, set it to the adapter's /dev/serial/by-id/... name.
ENTRYPOINT ["python", "burner_responder.py"]
CMD ["--fw-mode", "--live", "--live-interval", "1", "--serve-host", "0.0.0.0", \
     "--data-dir", "/app/data", "--log", "/app/data/responder.log", "--no-log-frames"]
