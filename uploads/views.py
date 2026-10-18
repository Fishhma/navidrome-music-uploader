import hashlib
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
from django.views.decorators.http import require_http_methods

from .models import UploadPolicy, UploadRecord, UserUploadLimit
from .validators import UploadRejected, incoming_directory, inspect_audio, is_rar_name, is_zip_name, safe_basename, validate_archive, validate_rar

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
    return UploadRecord.objects.filter(user=user, status=UploadRecord.Status.SUCCESS, uploaded_at__gte=since).aggregate(total=Sum("size"))["total"] or 0


def write_to_library(stream, display_name, digest):
    basename = safe_basename(display_name)
    path = PurePosixPath(basename)
    target_dir = settings.UPLOAD_LIBRARY_DIR / "Uploads"
    target_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{path.stem[:120]}-{digest[:10]}-{uuid.uuid4().hex[:8]}{path.suffix.lower()}"
    final_path = target_dir / stored_name
    # Stage beside the destination so the final rename is atomic for Navidrome.
    fd, staged_path = tempfile.mkstemp(prefix=".upload-", suffix=".part", dir=target_dir)
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "wb") as staged:
            shutil.copyfileobj(stream, staged, length=1024 * 1024)
            staged.flush()
            os.fsync(staged.fileno())
        os.replace(staged_path, final_path)
    except Exception:
        try:
            os.unlink(staged_path)
        except FileNotFoundError:
            pass
        raise
    return stored_name


def record_failure(user, name, message, size=0):
    UploadRecord.objects.create(
        user=user,
        username_snapshot=user.get_username(),
        original_name=name[:500],
        size=max(0, size),
        status=UploadRecord.Status.FAILED,
        message=message[:300],
    )


def record_success(user, original_name, stored_name, size, digest):
    try:
        with transaction.atomic():
            UploadRecord.objects.create(
                user=user,
                username_snapshot=user.get_username(),
                original_name=original_name[:500],
                stored_name=stored_name,
                size=size,
                sha256=digest,
                status=UploadRecord.Status.SUCCESS,
                message="Added to the shared music library.",
            )
    except IntegrityError:
        (settings.UPLOAD_LIBRARY_DIR / "Uploads" / stored_name).unlink(missing_ok=True)
        raise UploadRejected("This audio file has already been uploaded.")


def process_audio(user, stream, original_name, size, policy):
    inspect_audio(stream, original_name)
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
        raise UploadRejected("This audio file has already been uploaded.")
    if size > policy.max_file_size:
        raise UploadRejected("The file exceeds the configured per-file size limit.")
    if used_quota(user) + size > quota_for(user, policy):
        raise UploadRejected("The user's rolling 24-hour upload quota would be exceeded.")
    stored_name = write_to_library(stream, original_name, digest)
    try:
        record_success(user, original_name, stored_name, size, digest)
    except Exception:
        (settings.UPLOAD_LIBRARY_DIR / "Uploads" / stored_name).unlink(missing_ok=True)
        raise


def process_archive(user, uploaded_file, original_name, policy, is_rar=False, password=None):
    archive, infos, _ = (
        validate_rar(uploaded_file, policy, password) if is_rar else validate_archive(uploaded_file, policy)
    )
    staged = []
    stored_files = []
    try:
        with tempfile.TemporaryDirectory(dir=incoming_directory()) as stage_dir:
            actual_audio_size = 0
            for index, info in enumerate(infos):
                member_name = PurePosixPath(info.filename.replace("\\", "/")).name
                if not member_name:
                    continue
                stage_path = os.path.join(stage_dir, str(index))
                digest_obj = hashlib.sha256()
                member_size = 0
                member_stream = archive.open(info, pwd=password) if is_rar else archive.open(info)
                with member_stream as source, open(stage_path, "wb") as member:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        member_size += len(chunk)
                        actual_audio_size += len(chunk)
                        if actual_audio_size > policy.max_archive_expanded_size:
                            archive_type = "RAR" if is_rar else "ZIP"
                            raise UploadRejected(f"The {archive_type} archive expands beyond the configured size limit.")
                        digest_obj.update(chunk)
                        member.write(chunk)
                if member_size > policy.max_file_size:
                    raise UploadRejected(f"{member_name}: file exceeds the configured per-file size limit.")
                digest = digest_obj.hexdigest()
                with open(stage_path, "rb") as member:
                    inspect_audio(member, member_name)
                if UploadRecord.objects.filter(sha256=digest, status=UploadRecord.Status.SUCCESS).exists():
                    raise UploadRejected(f"{member_name}: this audio file has already been uploaded.")
                staged.append((stage_path, f"{safe_basename(original_name)}/{member_name}", member_name, member_size, digest))
            if used_quota(user) + actual_audio_size > quota_for(user, policy):
                raise UploadRejected("The user's rolling 24-hour upload quota would be exceeded.")
            prepared = []
            for stage_path, member_original, member_name, size, digest in staged:
                with open(stage_path, "rb") as member:
                    stored_name = write_to_library(member, member_name, digest)
                    stored_files.append(stored_name)
                prepared.append((member_original, stored_name, size, digest))
            with transaction.atomic():
                for member_original, stored_name, size, digest in prepared:
                    UploadRecord.objects.create(
                        user=user,
                        username_snapshot=user.get_username(),
                        original_name=member_original[:500],
                        stored_name=stored_name,
                        size=size,
                        sha256=digest,
                        status=UploadRecord.Status.SUCCESS,
                        message=f"Extracted from {'RAR' if is_rar else 'ZIP'} and added to the shared music library.",
                    )
    except rarfile.PasswordRequired as exc:
        raise UploadRejected("Enter the RAR archive password.") from exc
    except rarfile.RarWrongPassword as exc:
        raise UploadRejected("The RAR password is incorrect.") from exc
    except rarfile.RarCannotExec as exc:
        raise UploadRejected("RAR extraction is unavailable on the server.") from exc
    except rarfile.RarCRCError as exc:
        raise UploadRejected("The RAR archive is damaged or the password is incorrect.") from exc
    except (rarfile.Error, zipfile.BadZipFile, RuntimeError) as exc:
        archive_type = "RAR" if is_rar else "ZIP"
        raise UploadRejected(f"The {archive_type} archive contains an unreadable audio file.") from exc
    except Exception:
        for stored_name in stored_files:
            (settings.UPLOAD_LIBRARY_DIR / "Uploads" / stored_name).unlink(missing_ok=True)
        raise
    finally:
        archive.close()
    return len(stored_files)


@login_required
def dashboard(request):
    records = UploadRecord.objects.filter(user=request.user)[:50]
    policy = get_policy()
    return render(
        request,
        "uploads/dashboard.html",
        {
            "records": records,
            "policy": policy,
            "quota": quota_for(request.user, policy),
            "used": used_quota(request.user),
        },
    )


@login_required
@require_http_methods(["POST"])
def upload_files(request):
    policy = get_policy()
    files = request.FILES.getlist("files")
    if not files:
        message = "Select at least one file."
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "errors": [message]}, status=400)
        messages.error(request, message)
        return redirect("dashboard")
    if len(files) > policy.max_files_per_request:
        message = f"At most {policy.max_files_per_request} files can be uploaded in one request."
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "errors": [message]}, status=400)
        messages.error(request, message)
        return redirect("dashboard")
    if sum(uploaded_file.size for uploaded_file in files) > settings.MAX_UPLOAD_REQUEST_BYTES:
        message = "The total upload request exceeds the server limit."
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "errors": [message]}, status=413)
        messages.error(request, message)
        return redirect("dashboard")

    results = []
    archive_password = request.POST.get("archive_password", "")
    for uploaded_file in files:
        name = safe_basename(uploaded_file.name)
        try:
            if uploaded_file.size > policy.max_file_size:
                raise UploadRejected("The file exceeds the configured per-file size limit.")
            if is_zip_name(name):
                count = process_archive(request.user, uploaded_file, name, policy)
                results.append({"name": name, "status": "success", "message": f"Added {count} audio file(s) from the ZIP archive."})
            elif is_rar_name(name):
                count = process_archive(request.user, uploaded_file, name, policy, is_rar=True, password=archive_password or None)
                results.append({"name": name, "status": "success", "message": f"Added {count} audio file(s) from the RAR archive."})
            else:
                process_audio(request.user, uploaded_file, name, uploaded_file.size, policy)
                results.append({"name": name, "status": "success", "message": "Added to the shared music library."})
        except UploadRejected as exc:
            record_failure(request.user, name, str(exc), uploaded_file.size)
            results.append({"name": name, "status": "failed", "message": str(exc)})
        except OSError:
            logger.exception("Upload storage failure for user id=%s", request.user.pk)
            record_failure(request.user, name, "Server storage is unavailable.", uploaded_file.size)
            results.append({"name": name, "status": "failed", "message": "Server storage is unavailable."})
        except Exception:
            logger.exception("Unexpected upload failure for user id=%s", request.user.pk)
            record_failure(request.user, name, "The server could not process this file.", uploaded_file.size)
            results.append({"name": name, "status": "failed", "message": "The server could not process this file."})

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": all(item["status"] == "success" for item in results), "results": results})
    for result in results:
        getattr(messages, "success" if result["status"] == "success" else "error")(request, f'{result["name"]}: {result["message"]}')
    return redirect("dashboard")
