FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    libsodium23 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data directories
RUN mkdir -p data/conversations data/analysis data/plans

# Bot-only container: no HTTP server, no Claude CLI for pipeline execution.
CMD ["python", "pipeline.py", "--bot"]
