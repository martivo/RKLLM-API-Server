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
    && rm -rf /var/lib/apt/lists/*

# Download RKLLM runtime library and RKNN runtime library
RUN wget -q -O /usr/lib/librkllmrt.so \
    https://raw.githubusercontent.com/airockchip/rknn-llm/main/rkllm-runtime/Linux/librkllm_api/aarch64/librkllmrt.so \
 && wget -q -O /usr/lib/librknnrt.so \
    https://raw.githubusercontent.com/airockchip/rknn_model_zoo/main/3rdparty/rknpu2/Linux/aarch64/librknnrt.so \
 && git clone --depth 1 --branch release-v1.2.3 https://github.com/airockchip/rknn-llm.git /tmp/rknn-llm \
    && cp /tmp/rknn-llm/rkllm-runtime/Linux/librkllm_api/aarch64/librkllmrt.so /usr/lib/librkllmrt.so \
    && rm -rf /tmp/rknn-llm \
    && ldconfig



# Install Python dependencies
# - flask + flask-cors + gunicorn: core server
# - numpy + Pillow: required for VL (vision-language) image processing
# - neonize: WhatsApp client for the optional built-in agent (wa_agent.py); needs libmagic1
# - prometheus-flask-exporter + prometheus-client: optional metrics (graceful degradation if absent, but include for full feature set)
RUN pip install --no-cache-dir \
    flask \
    flask-cors \
    gunicorn \
    numpy \
    Pillow \
    prometheus-flask-exporter \
    prometheus-client \
    neonize==0.10.0

WORKDIR /app

COPY api.py gunicorn.config.py healthcheck.py wa_agent.py entrypoint.sh ./

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
