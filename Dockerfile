FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg \
    fonts-dejavu-core fonts-dejavu-extra \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY resources/ ./resources/
COPY .env* ./

RUN mkdir -p ./cache/audio ./cache/images ./cache/pdf ./cache/ambient_stitched ./cache/demo

CMD ["python", "-m", "src.main"]
