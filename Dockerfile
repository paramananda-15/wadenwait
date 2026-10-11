# Multi-stage / flexible build for Wade or Wait
# Expected image size:
# - Without detector (INCLUDE_DETECTOR=0): ~220 MB
# - With detector (INCLUDE_DETECTOR=1, CPU PyTorch + weights): ~850 MB

FROM python:3.11-slim

ARG INCLUDE_DETECTOR=0
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    PERCEPTION_MODE=vlm \
    DB_BACKEND=sqlite \
    STORAGE_BACKEND=local \
    LLM_PROVIDER=openai_compat

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Conditionally install CPU-only PyTorch, Torchvision and bake in detector weights
RUN if [ "$INCLUDE_DETECTOR" = "1" ] ; then \
        pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu && \
        python -c "import torchvision; torchvision.models.detection.ssdlite320_mobilenet_v3_large(weights=torchvision.models.detection.SSDLite320_MobileNet_V3_Large_Weights.DEFAULT)" ; \
    fi

# Copy application source code
COPY app/ ./app/
COPY static/ ./static/

RUN mkdir -p /app/data/uploads && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
