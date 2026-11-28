FROM python:3.12-alpine

LABEL org.opencontainers.image.title="OVH Buyer (OVH Sniper)" \
      org.opencontainers.image.description="Zero-dependency OVHcloud Dedicated/Eco/VPS auto-buyer, two-tier scheduler, auto proxy pool & web console" \
      org.opencontainers.image.source="https://github.com/AtomBoyC/ovh-buyer" \
      org.opencontainers.image.url="https://github.com/AtomBoyC/ovh-buyer" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    HOST=0.0.0.0 \
    PORT=8888 \
    OVH_DATA_DIR=/app/data

RUN apk add --no-cache tzdata ca-certificates su-exec \
    && addgroup -g 1000 -S appgroup \
    && adduser -u 1000 -S appuser -G appgroup

WORKDIR /app

COPY run.py /app/run.py
COPY ovh_buyer /app/ovh_buyer
COPY scripts/docker-entrypoint.sh /app/docker-entrypoint.sh

RUN sed -i 's/\r$//' /app/docker-entrypoint.sh \
    && chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /app/data/cache \
    && chown -R appuser:appgroup /app

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import os, urllib.request; os.environ.get('HEADLESS','').lower() in ('1','true','yes','on') or urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8888') + '/api/health', timeout=3)" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["serve"]
