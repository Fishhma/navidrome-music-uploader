FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
ARG APP_UID=1000
ARG APP_GID=1000
# The official unrar (Debian non-free) is required: unar cannot decrypt some password-protected
# RAR3 archives (e.g. stored/-m0 albums) and returns corrupted data, which fails the CRC check.
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install --no-install-recommends -y unrar \
    && rm -rf /var/lib/apt/lists/* \
    && command -v unrar
RUN groupadd --gid "${APP_GID}" uploader \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --home-dir /app --no-create-home --shell /usr/sbin/nologin uploader
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=uploader:uploader . .
RUN mkdir -p /data /incoming /music && chown uploader:uploader /data /incoming /music
RUN DJANGO_SECRET_KEY=build-only-static-collection-key python manage.py collectstatic --noinput
RUN chown -R uploader:uploader /app/staticfiles
USER uploader
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn uploader_project.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 4 --worker-class gthread --worker-tmp-dir /tmp --timeout 900 --access-logfile -"]
