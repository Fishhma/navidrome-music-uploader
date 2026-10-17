FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends unar gettext \
    && rm -rf /var/lib/apt/lists/*
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "${APP_GID}" uploader \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --home-dir /app --no-create-home --shell /usr/sbin/nologin uploader
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=uploader:uploader . .
RUN mkdir -p /data /incoming /music && chown uploader:uploader /data /incoming /music
RUN DJANGO_SECRET_KEY=build-only-static-collection-key python manage.py compilemessages && DJANGO_SECRET_KEY=build-only-static-collection-key python manage.py collectstatic --noinput
RUN chown -R uploader:uploader /app/staticfiles
USER uploader
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn uploader_project.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 120"]
