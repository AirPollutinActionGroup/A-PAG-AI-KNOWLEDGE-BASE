FROM python:3.13-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    # OpenCV, which RapidOCR uses, links against these. The slim base image has neither, and
    # without them `import cv2` fails at worker start with a bare "libGL.so.1: cannot open
    # shared object file" that names nothing to do with OCR.
    libgl1 \
    libglib2.0-0 \
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

# The reranker, for exactly the same reason — and it was missed at first, which showed up as
# "Fetching 5 files" in the API's startup log and 16 seconds of HuggingFace download in front of
# the first question anyone asked. Every fresh container paid it again, and a deployment without
# egress to huggingface.co would never have got a reranked result at all.
#
# Uses the same RERANK_MODEL default as src/core/config.py. If that default changes, this must
# change with it, or the model is fetched at runtime after all.
RUN python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder; TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2')"

# Same for the OCR models, for the same reason. Constructing RapidOCR resolves and downloads all
# three (detection, classification, recognition) into site-packages, which `COPY . .` below does
# not disturb. ~15MB, against a worker that would otherwise reach for the network the first time
# it met a scanned page — mid-job, inside a lease.
RUN python -c "from rapidocr import RapidOCR; RapidOCR()"

# Copy application source code
COPY . .

# Default command
CMD ["python", "main.py"]
