# syntax=docker/dockerfile:1.6
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Minimal system deps (curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# If you keep requirements.txt, uncomment the next two lines and remove the direct pip install below
# COPY requirements.txt .
# RUN pip install -r requirements.txt

# add tzdata (PyPI) to the pip line
RUN pip install --upgrade pip \
 && pip install fastapi uvicorn[standard] python-dotenv redis rq ib_insync tzdata

# Bring in your app code
COPY . /app

# Optional: non-root user
RUN useradd -m appuser
USER appuser

# Expose FastAPI
EXPOSE 8000

# Start the FastAPI app (adjust module if your app object is elsewhere)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
