# Pipeline_UI — Backend (FastAPI)
#
# Minimal, Docker Desktop friendly image. Application-layer service ONLY:
# it never joins the internal attack networks. It reaches Caldera over the
# compose management network (http://caldera-c2:8888) and reads the
# Docker Engine API (socket, read-only) for isolation inspection.
#
# NOTE: docker-compose bind-mounts the repository root read-only at /repo and
#   sets the working directory to /repo/apps/backend (hot reload + Alembic).
#   The image itself only provides the Python environment + dependencies.
#   Settings (Caldera URL/key, database) come from PIPELINE_UI_* env vars.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching. Copying pyproject.toml
# (and the README it references) lets `pip install` resolve the project metadata.
COPY pyproject.toml README.md ./
COPY app ./app

# Install the backend package plus its runtime dependencies.
RUN pip install --upgrade pip \
    && pip install .

# Default working directory is overridden by compose to /repo/apps/backend.
# When run standalone (no repo mount) the image still boots the API.
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
