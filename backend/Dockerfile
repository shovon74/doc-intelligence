FROM python:3.12-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies: tesseract, poppler, OpenCV/libgl, libmagic, curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    poppler-utils \
    libgl1 \
    libglib2.0-0 \
    libmagic1 \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install wheel
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Copy pyproject.toml first for layer caching
COPY pyproject.toml .

# Install dependencies (including dev for test runner)
RUN pip install --no-cache-dir -e ".[dev]"

# Unprivileged user owns the upload directory; nothing runs as root
RUN useradd --create-home --uid 10001 app     && mkdir -p /data/uploads && chown -R app:app /data/uploads

# Copy application source code
COPY --chown=app:app . .
USER app

# Expose FastAPI port
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
