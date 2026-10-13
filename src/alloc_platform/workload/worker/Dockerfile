FROM python:3.11-slim

# Create dedicated non-root user and group
RUN groupadd -r worker && useradd -r -g worker -d /home/worker -s /sbin/nologin worker \
    && mkdir -p /home/worker /app \
    && chown -R worker:worker /home/worker /app

WORKDIR /app

# Install minimal server requirements
RUN pip install --no-cache-dir fastapi==0.110.0 uvicorn==0.28.0 pydantic==2.6.0

COPY --chown=worker:worker app.py /app/app.py

USER worker

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

ENTRYPOINT ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
