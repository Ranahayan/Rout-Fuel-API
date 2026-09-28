# Production image: gunicorn serving the API + map page. SQLite lives on the
# volume mounted at /app/var (see docker-compose.yml); the fuel data is loaded
# idempotently at start-up. Admin static files are not served (the API and the
# map page need none); put a static file server in front if you want /admin.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/app/var/db.sqlite3

WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

COPY . .
RUN useradd --create-home app && mkdir -p /app/var && chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=4).status == 200 else 1)" || exit 1

CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py load_fuel_prices && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers ${WEB_CONCURRENCY:-2} --timeout 30 --access-logfile -"]
