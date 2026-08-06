FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

COPY apps/api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# Migrations run in the API's own startup (lifespan) now — before it serves, and
# owned by the API alone so it never races the worker over Alembic. That covers
# a bare `uvicorn` too, not just this image. Set AUTO_MIGRATE=false to skip it
# where migrations are applied out of band.
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
