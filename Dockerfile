FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    libgomp1 \
    git \
    file \
    curl \
    libmagic1 \
    iputils-ping \
    iproute2 \
    dnsutils \
    netcat-openbsd \
    procps \
    jq \
    && rm -rf /var/lib/apt/lists/*

# RKLLM runtime version (a release tag of airockchip/rknn-llm without the
# "release-" prefix). api.py's ctypes structs match the 1.2.x header; try
# another version with: docker build --build-arg RKLLM_VERSION=v1.3.0 .
ARG RKLLM_VERSION=v1.2.3

# Download the RKLLM runtime library (pinned) and the RKNN runtime library
RUN wget -q -O /usr/lib/librkllmrt.so \
    https://raw.githubusercontent.com/airockchip/rknn-llm/release-${RKLLM_VERSION}/rkllm-runtime/Linux/librkllm_api/aarch64/librkllmrt.so \
 && wget -q -O /usr/lib/librknnrt.so \
    https://raw.githubusercontent.com/airockchip/rknn_model_zoo/main/3rdparty/rknpu2/Linux/aarch64/librknnrt.so \
 && ldconfig



# Install Python dependencies
# - flask + flask-cors + gunicorn: core server
# - numpy + Pillow: required for VL (vision-language) image processing
# - neonize: WhatsApp client for the optional built-in agent (wa_agent.py); needs libmagic1
# - pyyaml: compose file parsing in tools/image-updates
# - prometheus-flask-exporter + prometheus-client: optional metrics (graceful degradation if absent, but include for full feature set)
RUN pip install --no-cache-dir \
    flask \
    flask-cors \
    gunicorn \
    numpy \
    Pillow \
    prometheus-flask-exporter \
    prometheus-client \
    neonize==0.10.0 \
    pyyaml

# Docker CLI + compose plugin for the agent's shell tool (client only; mount
# /var/run/docker.sock to use it)
COPY --from=docker:29.9.3-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:29.9.3-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
# Trivy for the CVE counts in tools/image-updates
COPY --from=aquasec/trivy:0.76.2 /usr/local/bin/trivy /usr/local/bin/trivy

WORKDIR /app

COPY api.py gunicorn.config.py healthcheck.py wa_agent.py entrypoint.sh ./
# Helper commands for the agent's shell tool
COPY --chmod=755 tools/image-updates tools/image-apply /usr/local/bin/

# Models are mounted at runtime — do not bake into image
VOLUME /root/models

EXPOSE 8000

ENV RKLLM_LOG_LEVEL=1 \
    RKLLM_API_LOG_LEVEL=INFO \
    RKLLM_MODELS_ROOT=/root/models \
    GUNICORN_BIND=0.0.0.0:8000 \
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=4 \
    GUNICORN_TIMEOUT=300 \
    PYTHONUNBUFFERED=1 \
    AGENT_ENABLED=0

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python /app/healthcheck.py

# entrypoint.sh starts the optional WhatsApp agent (AGENT_ENABLED=1) next to gunicorn
CMD ["sh", "/app/entrypoint.sh"]
