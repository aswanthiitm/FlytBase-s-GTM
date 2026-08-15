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

# One image, two roles, dispatched on an explicit variable rather than on a
# per-service start command — so the role a container is playing is visible in
# its own environment instead of buried in host settings.
#   GTM_ROLE=web   -> dashboard
#   anything else  -> poller (the default, because keeping the store current
#                     unattended is this image's primary job)
CMD ["sh", "-c", "if [ \"$GTM_ROLE\" = web ]; then exec python -m gtm serve; else exec python -m gtm poll --every ${GTM_POLL_SECONDS:-300} --source ${GTM_SOURCE:-flytbase}; fi"]
