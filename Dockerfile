FROM python:3.11-slim

WORKDIR /app

# No apt deps needed — psycopg[binary] and bcrypt both ship pre-compiled
# wheels. Textbook ingestion (docling) runs in the dedicated
# ingest-worker image (Dockerfile.ingest-worker), so this image stays
# slim — no Tesseract / poppler / docling here.

# ── Install Python deps (cached unless pyproject.toml changes) ──
COPY pyproject.toml .
# Stub backend package so pip can resolve deps without the full source tree
RUN mkdir -p backend && touch backend/__init__.py
RUN pip install --no-cache-dir -e .

# ── Copy application source ──────────────────────────────────────
COPY backend/  backend/
# COPY frontend/ frontend/
COPY alembic.ini .
COPY entrypoint.sh .

# Ensure Unix line endings and executable bit (safe on any build OS)
RUN sed -i 's/\r//' entrypoint.sh && chmod +x entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
