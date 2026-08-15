FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so editing code does not invalidate the install layer.
# Note we install from requirements.txt rather than `pip install .` — this is a
# flat layout with no build backend, which is exactly what broke the Nixpacks
# build.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY gtm ./gtm
COPY fixtures ./fixtures

# Default to the poller: this image's whole job is to keep the store current
# without anyone touching it. Railway's per-service start command overrides
# this when the same image also serves the dashboard.
CMD ["python", "-m", "gtm", "poll", "--every", "300", "--source", "flytbase"]
