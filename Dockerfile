FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a code change does not invalidate the layer.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "groq>=0.11" \
      "psycopg[binary]>=3.2" \
      "httpx>=0.27" \
      "pydantic>=2.7" \
      "python-dotenv>=1.0" \
      "typer>=0.12" \
      "rich>=13.7"

COPY gtm ./gtm
COPY fixtures ./fixtures

# Flat layout: `python -m gtm` works because /app is on sys.path.
# The start command is set per Railway service:
#   worker -> python -m gtm poll --every 300 --source flytbase
#   web    -> python -m gtm serve --host 0.0.0.0 --port $PORT
CMD ["python", "-m", "gtm", "status"]
