FROM python:3.13-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Pre-download the embedding model into the image.
#
# A worker that fetches weights on first boot fails closed on a network blip and is not
# reproducible — two containers built from the same tag could end up running different weights.
# Baking it in makes the image larger and the build slower, which is the right trade for a
# worker that must start reliably in an environment with no guaranteed egress.
#
# Uses the same EMBEDDING_MODEL default as src/core/config.py. If that default changes, this
# must change with it, or the model is downloaded at runtime after all.
ENV FASTEMBED_CACHE_PATH=/app/.model_cache
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-base-en-v1.5')"

# Copy application source code
COPY . .

# Default command
CMD ["python", "main.py"]
