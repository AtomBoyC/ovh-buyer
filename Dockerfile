FROM python:3.12-alpine

LABEL org.opencontainers.image.title="OVH Buyer (OVH Sniper)" \
      org.opencontainers.image.description="OVHcloud Black Friday Eco/Dedicated/VPS auto-buyer, stock radar & zero-dependency web console" \
      org.opencontainers.image.source="https://github.com/AtomBoyC/ovh-buyer" \
      org.opencontainers.image.url="https://github.com/AtomBoyC/ovh-buyer" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    HOST=0.0.0.0 \
    PORT=8888 \
    OVH_DATA_DIR=/app/data

RUN apk add --no-cache tzdata ca-certificates \
    && addgroup -S appgroup \
    && adduser -S appuser -G appgroup

WORKDIR /app

COPY run.py /app/run.py
COPY ovh_buyer /app/ovh_buyer

RUN mkdir -p /app/data && chown -R appuser:appgroup /app

USER appuser

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8888/api/health', timeout=3)" || exit 1

ENTRYPOINT ["python3", "/app/run.py"]
CMD ["serve"]
