FROM python:3.13-slim-bookworm

COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.0.1 /lambda-adapter /opt/extensions/lambda-adapter

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/api/health

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 interview

WORKDIR /app
COPY --chown=interview:interview backend/ backend/
COPY --chown=interview:interview database/ database/

USER interview
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ['PORT'] + '/api/health', timeout=2)"

CMD ["python3", "backend/server.py", "--host", "0.0.0.0"]
