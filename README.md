# Navidrome Music Uploader

Standalone Django web app for authenticated uploads into an **existing** Navidrome music directory. It does not create, replace, or configure Navidrome. The Compose service publishes port `4580` to the home network(for now) and is intended for trusted LAN access or separately secured access.

## Features

- Separate Django accounts, password hashing, CSRF-protected login/logout, and per-user upload history.
- Django Admin user enable/disable, global upload policy, optional per-user rolling 24-hour quota, and full upload history.
- Multi-file selection, drag-and-drop, recursive folder selection, transfer progress, and result messages.
- Content-signature checks for supported audio formats and cover images (JPG/JPEG, PNG, WEBP, GIF, BMP).
- ZIP support for unencrypted archives, traditional ZipCrypto, and AES-encrypted ZIP files. RAR/RAR5 support uses `rarfile` with the `unar` backend; password-protected RAR support depends on archive variant and backend capabilities.
- Archive paths are validated and preserved under a unique collection folder so album cover art stays next to related music. Non-media files are ignored but still count toward the 100-file limit and archive size checks.
- Private SQLite and incoming-upload volumes; validated media files are written atomically into `/music/Uploads/`. The application does not chown or loosen permissions on the shared library.

## Prepare the existing server

The Compose bind mount is configured for the supplied host path `/path/to/navidrome/music`; change only that mount source if your existing library path differs. Do not recreate or edit the Navidrome service.

The container runs with the configured `DJANGO_UID`/`DJANGO_GID` (defaults `1000:1000`), matching the reported Navidrome owner. On the host, verify the existing library path and allow that account to write to it; do not use broad `chmod 777`:

```sh
id
stat -c '%U:%G %a %n' /path/to/navidrome/music
```

If this UID/GID does not already have the required directory write permission, coordinate a narrowly scoped host ownership/group or ACL change with the server administrator. The application intentionally leaves permissions and ownership inherited from the host directory.

## Run with Docker Compose

1. Copy `.env.example` to `.env`. Set `DJANGO_SECRET_KEY` to a cryptographically random secret (for example, `python3 -c 'import secrets; print(secrets.token_urlsafe(64))'`). Keep `.env` private; it is gitignored.
2. Set `DJANGO_ALLOWED_HOSTS` to the hostnames/IP addresses used to access the app. The default includes `192.168.0.110`; change it if your server address differs. Port 4580 is published on the host network, so restrict access to the trusted LAN using the host firewall.
3. From this directory, build and start the standalone container:

   ```sh
   docker compose -f docker-compose.yml up -d --build
   docker compose -f docker-compose.yml logs -f music-uploader
   ```

4. Create the initial Django administrator:

   ```sh
   docker compose -f docker-compose.yml exec music-uploader python manage.py createsuperuser
   ```

5. Open `http://192.168.x.x:4580/`, then visit `/admin/` to add individual users and tune the upload policy.

For HTTPS behind a trusted TLS-terminating reverse proxy, configure the proxy separately, set `DJANGO_SECURE_COOKIES=1`, set `DJANGO_CSRF_TRUSTED_ORIGINS` to the exact `https://` origin, and configure the correct allowed host. Do not expose this service directly to the public internet. Compose uses persistent named volumes `uploader_data` and `uploader_incoming`; back them up along with `.env`. The media files themselves remain in the existing Navidrome library.

Navidrome sees new tracks under its existing mounted library at its next library scan. Keep its existing scan configuration unchanged; trigger a scan through your normal Navidrome mechanism if required.

## Limits and operational notes

- Defaults: 1 GiB per uploaded file/archive, 2 GiB total HTTP request, 100 selected files per request, 100 file entries per archive, 4 GiB maximum expanded size per archive, and maximum 100:1 expansion ratio. The default rolling 24-hour per-user quota remains 1 GiB and is intentionally unchanged. Administrators can tune size limits in Django Admin; per-user quota overrides remain under **User upload limits**.
- All non-directory files inside ZIP/RAR archives count toward the 100-entry limit, including images and ignored documents. Directory records do not count. Folder selection counts all files, including unsupported files; unsupported non-media files are skipped.
- Folder and archive relative paths are kept under a unique collection folder. Supported cover images are stored with their original filenames, while audio filenames are sanitized and given a unique suffix. Duplicate-content detection remains global across successful uploads.
- Encrypted archives prompt for a password only when needed. Passwords are sent only for the retry request and are not stored in upload history. Use HTTPS if credentials must be protected from other devices on the network.
- Nested archives inside a selected folder are not unpacked. Transcoding and audio metadata parsing are not supported. Validation checks file signatures, not full codec/image integrity; Navidrome remains responsible for catalog indexing.
- Validation checks file signatures, not full codec integrity. The Navidrome scanner remains responsible for catalog indexing.
- A user with Django staff privileges can access Django Admin; grant that role only to trusted administrators. User accounts are created by an admin; public self-registration is not provided.
- The container has no root privileges, drops Linux capabilities, uses read-only root filesystem, and has memory/PID limits. Restrict access at the host firewall and reverse proxy as appropriate.

## Development and tests

```sh
python -m pip install -r requirements.txt
export DJANGO_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')"
python manage.py migrate
python manage.py test
```

Tests use an isolated temporary SQLite database and temporary upload/library folders. The upload request body is capped at 2 GiB by the streaming Django upload handler, with individual files capped at 1 GiB; the reverse-proxy port binding and host network policy should remain restrictive.
