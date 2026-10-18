import hashlib
import json
import logging
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import timedelta
from pathlib import PurePosixPath

import rarfile
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from .models import UploadPolicy, UploadRecord, UserUploadLimit
from .validators import (
    AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, ArchivePasswordInvalid,
    ArchivePasswordRequired, UploadRejected, incoming_directory,
    inspect_audio, inspect_image, safe_basename, safe_relative_path,
    validate_archive, validate_rar,
)

logger = logging.getLogger(__name__)


def get_policy():
    policy, _ = UploadPolicy.objects.get_or_create(pk=1)
    return policy


def quota_for(user, policy):
    try:
        override = user.upload_limit.daily_quota
    except UserUploadLimit.DoesNotExist:
        override = None
    return override if override is not None else policy.daily_quota


def used_quota(user):
    since = timezone.now() - timedelta(hours=24)
    return UploadRecord.objects.filter(
        user=user, status=UploadRecord.Status.SUCCESS, uploaded_at__gte=since
    ).aggregate(total=Sum("size"))["total"] or 0


def _stored_path(stored_name):
    root = (settings.UPLOAD_LIBRARY_DIR / "Uploads").resolve()
    target = (root / safe_relative_path(stored_name)).resolve()
    if not target.is_relative_to(root):
        raise UploadRejected(_("The destination path is unsafe."))
    return target


def _remove_stored(stored_name):
    try:
        target = _stored_path(stored_name)
        target.unlink(missing_ok=True)
        parent = target.parent
        root = (settings.UPLOAD_LIBRARY_DIR / "Uploads").resolve()
        while parent != root and parent.is_relative_to(root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    except (OSError, UploadRejected):
        logger.exception("Could not remove staged library file")


def write_to_library(stream, display_name, digest, *, relative_path=None, collection=None, preserve_basename=False):
    root = settings.UPLOAD_LIBRARY_DIR / "Uploads"
    root.mkdir(parents=True, exist_ok=True)
    target_root = root / safe_basename(collection) if collection else root
    if relative_path:
        rel = safe_relative_path(relative_path)
        target_dir = target_root.joinpath(*rel.parts[:-1])
        basename = rel.name
    else:
        target_dir = target_root
        basename = safe_basename(display_name)
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve()
    resolved_dir = target_dir.resolve()
    if not resolved_dir.is_relative_to(resolved_root):
        raise UploadRejected(_("The destination path is unsafe."))
    path = PurePosixPath(basename)
    if not preserve_basename:
        basename = f"{path.stem[:100]}-{digest[:10]}-{uuid.uuid4().hex[:8]}{path.suffix.lower()}"
    final_path = target_dir / basename
    fd, staged_path = tempfile.mkstemp(prefix=".upload-", suffix=".part", dir=target_dir)
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "wb") as staged:
            shutil.copyfileobj(stream, staged, length=1024 * 1024)
            staged.flush()
            os.fsync(staged.fileno())
        # Hard-link is atomic and fails instead of overwriting an existing file.
        os.link(staged_path, final_path)
        os.unlink(staged_path)
    except Exception:
        try:
            os.unlink(staged_path)
        except FileNotFoundError:
            pass
        raise
    return final_path.resolve().relative_to(resolved_root).as_posix()


def record_failure(user, name, message, size=0):
    UploadRecord.objects.create(
        user=user, username_snapshot=user.get_username(), original_name=name[:500],
        size=max(0, size), status=UploadRecord.Status.FAILED, message=message[:300],
    )


def record_success(user, original_name, stored_name, size, digest, message=None):
    try:
        with transaction.atomic():
            UploadRecord.objects.create(
                user=user, username_snapshot=user.get_username(),
                original_name=original_name[:500], stored_name=stored_name,
                size=size, sha256=digest, status=UploadRecord.Status.SUCCESS,
                message=message or _("Added to the shared music library."),
            )
    except IntegrityError as exc:
        _remove_stored(stored_name)
        raise UploadRejected(_("This file has already been uploaded.")) from exc


def process_media(user, stream, original_name, size, policy, *, relative_path=None, collection=None, image=False):
    inspect_image(stream, original_name) if image else inspect_audio(stream, original_name)
    stream.seek(0)
    digest_obj = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        digest_obj.update(chunk)
    digest = digest_obj.hexdigest()
    stream.seek(0)
    if UploadRecord.objects.filter(sha256=digest, status=UploadRecord.Status.SUCCESS).exists():
        raise UploadRejected(_("This file has already been uploaded."))
    if size > policy.max_file_size:
        raise UploadRejected(_("The file exceeds the configured per-file size limit."))
    if used_quota(user) + size > quota_for(user, policy):
        raise UploadRejected(_("The user's rolling 24-hour upload quota would be exceeded."))
    stored_name = write_to_library(
        stream, original_name, digest, relative_path=relative_path,
        collection=collection, preserve_basename=image and bool(relative_path),
    )
    try:
        record_success(user, original_name, stored_name, size, digest,
                       _("Added cover image to the shared music library.") if image else None)
    except Exception:
        _remove_stored(stored_name)
        raise


def _process_archive(user, uploaded_file, original_name, policy, kind, password=None):
    staged, stored_files, archive = [], [], None
    temp_context = tempfile.TemporaryDirectory(dir=incoming_directory())
    try:
        if kind == "rar":
            archive_path = os.path.join(temp_context.name, "upload.rar")
            uploaded_file.seek(0)
            with open(archive_path, "wb") as destination:
                shutil.copyfileobj(uploaded_file, destination, length=1024 * 1024)
            archive, infos, _ = validate_rar(archive_path, policy, password=password)
        else:
            archive, infos, _ = validate_archive(uploaded_file, policy, password=password)

        collection = f"{safe_basename(os.path.splitext(original_name)[0])}-{uuid.uuid4().hex[:8]}"
        media_size = 0
        for index, info in enumerate(infos):
            relative = safe_relative_path(info.filename)
            member_name = relative.name
            image = relative.suffix.lower() in IMAGE_EXTENSIONS
            stage_path = os.path.join(temp_context.name, f"member-{index}")
            digest_obj, member_size = hashlib.sha256(), 0
            try:
                with archive.open(info) as source, open(stage_path, "wb") as member:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        member_size += len(chunk)
                        media_size += len(chunk)
                        if media_size > policy.max_archive_expanded_size:
                            raise UploadRejected(_("The archive expands beyond the configured size limit."))
                        if member_size > policy.max_file_size:
                            raise UploadRejected(_("%(name)s: file exceeds the configured per-file size limit.") % {"name": member_name})
                        digest_obj.update(chunk)
                        member.write(chunk)
            except (zipfile.BadZipFile, RuntimeError, rarfile.Error) as exc:
                if password:
                    raise ArchivePasswordInvalid(_("The archive password is incorrect or the archive is damaged.")) from exc
                raise UploadRejected(_("The archive contains an unreadable media file.")) from exc

            digest = digest_obj.hexdigest()
            with open(stage_path, "rb") as member:
                inspect_image(member, member_name) if image else inspect_audio(member, member_name)
            if UploadRecord.objects.filter(sha256=digest, status=UploadRecord.Status.SUCCESS).exists():
                raise UploadRejected(_("%(name)s: this file has already been uploaded.") % {"name": member_name})
            staged.append((stage_path, relative, member_name, member_size, digest, image))

        if used_quota(user) + media_size > quota_for(user, policy):
            raise UploadRejected(_("The user's rolling 24-hour upload quota would be exceeded."))

        prepared = []
        for stage_path, relative, member_name, size, digest, image in staged:
            with open(stage_path, "rb") as member:
                stored_name = write_to_library(member, member_name, digest, relative_path=relative,
                                               collection=collection, preserve_basename=image)
            stored_files.append(stored_name)
            prepared.append((f"{original_name}/{relative.as_posix()}", stored_name, size, digest, image))

        with transaction.atomic():
            for original, stored_name, size, digest, image in prepared:
                UploadRecord.objects.create(
                    user=user, username_snapshot=user.get_username(),
                    original_name=original[:500], stored_name=stored_name, size=size,
                    sha256=digest, status=UploadRecord.Status.SUCCESS,
                    message=_("Extracted media and cover art from archive.") if image else _("Extracted from archive and added to the shared music library."),
                )
    except (ArchivePasswordRequired, ArchivePasswordInvalid):
        for stored_name in stored_files:
            _remove_stored(stored_name)
        raise
    except Exception:
        for stored_name in stored_files:
            _remove_stored(stored_name)
        raise
    finally:
        if archive:
            archive.close()
        temp_context.cleanup()
    return len(stored_files)


def process_zip(user, uploaded_file, original_name, policy, password=None):
    return _process_archive(user, uploaded_file, original_name, policy, "zip", password=password)


def process_rar(user, uploaded_file, original_name, policy, password=None):
    return _process_archive(user, uploaded_file, original_name, policy, "rar", password=password)


@login_required
def dashboard(request):
    records = UploadRecord.objects.filter(user=request.user)[:50]
    policy = get_policy()
    return render(request, "uploads/dashboard.html", {
        "records": records, "policy": policy,
        "quota": quota_for(request.user, policy), "used": used_quota(request.user),
    })


@login_required
@require_http_methods(["POST"])
def upload_files(request):
    policy = get_policy()
    files = request.FILES.getlist("files")
    folder_upload = request.POST.get("folder_upload") == "1"
    relative_paths = []
    if folder_upload:
        try:
            raw_paths = json.loads(request.POST.get("relative_paths", "[]"))
            if not isinstance(raw_paths, list) or len(raw_paths) != len(files) or not all(isinstance(p, str) for p in raw_paths):
                raise ValueError("invalid relative path list")
            relative_paths = [safe_relative_path(path) for path in raw_paths]
            if len({path.parts[0] for path in relative_paths}) > 1:
                raise UploadRejected(_("Select one folder at a time."))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            message = str(exc) if isinstance(exc, UploadRejected) else _("The selected folder paths are invalid.")
            return JsonResponse({"ok": False, "errors": [message]}, status=400)

    if not files:
        message = _("Select at least one file.")
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "errors": [message]}, status=400)
        messages.error(request, message)
        return redirect("dashboard")
    if len(files) > policy.max_files_per_request:
        message = _("At most %(max_files)s files can be uploaded in one request.") % {"max_files": policy.max_files_per_request}
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "errors": [message]}, status=400)
        messages.error(request, message)
        return redirect("dashboard")
    if sum(item.size for item in files) > settings.MAX_UPLOAD_REQUEST_BYTES:
        message = _("The total upload request exceeds the server limit.")
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "errors": [message]}, status=413)
        messages.error(request, message)
        return redirect("dashboard")

    folder_collection = None
    if folder_upload and relative_paths:
        folder_collection = f"{safe_basename(relative_paths[0].parts[0])}-{uuid.uuid4().hex[:8]}"

    results = []
    for index, uploaded_file in enumerate(files):
        name = safe_basename(uploaded_file.name)
        password = request.POST.get(f"archive_password_{index}") or None
        try:
            if uploaded_file.size > policy.max_file_size:
                raise UploadRejected(_("The file exceeds the configured per-file size limit."))
            extension = os.path.splitext(name.lower())[1]
            if extension in {".zip", ".rar"}:
                if folder_upload:
                    results.append({"index": index, "name": name, "status": "ignored", "message": _("Nested archives are not unpacked from folder uploads.")})
                    continue
                count = (process_zip if extension == ".zip" else process_rar)(
                    request.user, uploaded_file, name, policy, password=password
                )
                label = "ZIP" if extension == ".zip" else "RAR"
                results.append({"index": index, "name": name, "status": "success", "message": _("Added %(count)s media file(s) from the %(kind)s archive.") % {"count": count, "kind": label}})
                continue

            if folder_upload:
                relative = relative_paths[index]
                relative_inside = PurePosixPath(*relative.parts[1:]) if len(relative.parts) > 1 else PurePosixPath(name)
                if extension in AUDIO_EXTENSIONS:
                    process_media(request.user, uploaded_file, relative.as_posix(), uploaded_file.size, policy,
                                  relative_path=relative_inside, collection=folder_collection)
                elif extension in IMAGE_EXTENSIONS:
                    process_media(request.user, uploaded_file, relative.as_posix(), uploaded_file.size, policy,
                                  relative_path=relative_inside, collection=folder_collection, image=True)
                else:
                    results.append({"index": index, "name": relative.as_posix(), "status": "ignored", "message": _("Ignored unsupported non-media file.")})
                    continue
                results.append({"index": index, "name": relative.as_posix(), "status": "success", "message": _("Added to the shared music library.")})
                continue

            if extension in IMAGE_EXTENSIONS:
                process_media(request.user, uploaded_file, name, uploaded_file.size, policy, image=True)
                results.append({"index": index, "name": name, "status": "success", "message": _("Added cover image to the shared music library.")})
            else:
                process_media(request.user, uploaded_file, name, uploaded_file.size, policy)
                results.append({"index": index, "name": name, "status": "success", "message": _("Added to the shared music library.")})
        except ArchivePasswordRequired as exc:
            results.append({"index": index, "name": name, "status": "needs_password", "needs_password": True, "message": str(exc)})
        except ArchivePasswordInvalid as exc:
            results.append({"index": index, "name": name, "status": "needs_password", "needs_password": True, "password_invalid": True, "message": str(exc)})
        except UploadRejected as exc:
            record_failure(request.user, name, str(exc), uploaded_file.size)
            results.append({"index": index, "name": name, "status": "failed", "message": str(exc)})
        except (rarfile.RarWrongPassword, rarfile.PasswordRequired) as exc:
            results.append({"index": index, "name": name, "status": "needs_password", "needs_password": True,
                            "password_invalid": bool(password), "message": _("The archive password is incorrect or required.")})
        except OSError:
            logger.exception("Upload storage failure for user id=%s", request.user.pk)
            record_failure(request.user, name, _("Server storage is unavailable."), uploaded_file.size)
            results.append({"index": index, "name": name, "status": "failed", "message": _("Server storage is unavailable.")})
        except Exception:
            logger.exception("Unexpected upload failure for user id=%s", request.user.pk)
            record_failure(request.user, name, _("The server could not process this file."), uploaded_file.size)
            results.append({"index": index, "name": name, "status": "failed", "message": _("The server could not process this file.")})

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": all(item["status"] in {"success", "ignored"} for item in results), "results": results})
    for result in results:
        getattr(messages, "success" if result["status"] in {"success", "ignored"} else "error")(
            request, f'{result["name"]}: {result["message"]}'
        )
    return redirect("dashboard")
