FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# LibreOffice, headless, to turn a deck or a Word document into a PDF at
# ingest — which is what lets a citation from slide 34 show slide 34. Nothing
# else here can draw one: a .pptx is shapes, masters, themes and fonts, and
# rendering that back into the picture a person saw is a presentation program's
# whole job.
#
# The filters, not the applications: --no-install-recommends drops the Java
# stack, the sample documents and the desktop integration, which is most of the
# weight. Fonts are included deliberately — without them every converted slide
# falls back to a substitute and the picture stops matching the document it
# claims to be of.
#
# Absence is handled, not assumed: packages/core/render.py checks for the
# binary, and a build without it simply has no page pictures for Office files,
# exactly as before this existed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-impress \
        libreoffice-writer \
        libreoffice-calc \
        fonts-dejavu-core \
        fonts-liberation \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY apps/api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# Migrations run in the API's own startup (lifespan) now — before it serves, and
# owned by the API alone so it never races the worker over Alembic. That covers
# a bare `uvicorn` too, not just this image. Set AUTO_MIGRATE=false to skip it
# where migrations are applied out of band.
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
