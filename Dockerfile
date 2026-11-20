FROM python:3.12-alpine
LABEL org.opencontainers.image.title="Picture House" \
      org.opencontainers.image.description="Your Plex library as a phone app: posters, barcode scanner, wish list, works offline." \
      org.opencontainers.image.licenses="MIT"
WORKDIR /app
COPY picture-house.py picture-house-server.py icon.svg LICENSE ./
COPY app ./app
ENV PH_DATA=/data PH_PLEX=http://host.docker.internal:32400 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- http://127.0.0.1:8765/healthz || exit 1
CMD ["python3", "/app/picture-house-server.py", "--port", "8765"]
