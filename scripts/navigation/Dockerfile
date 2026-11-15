FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib DATA_DIR=/data
WORKDIR /app
COPY scripts/navigation/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && useradd --uid 10001 --create-home navigation \
    && mkdir /data && chown navigation:navigation /data
COPY scripts/navigation /app/scripts/navigation
USER navigation
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=3).read()"
CMD ["python", "-m", "scripts.navigation.service"]
