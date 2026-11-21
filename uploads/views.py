import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from .models import Upload, UploadRecord
from .processing import UploadBatch, get_policy, process_upload, quota_for, used_quota

logger = logging.getLogger(__name__)


def _is_ajax(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _reject(request, message, status):
    if _is_ajax(request):
        return JsonResponse({"ok": False, "errors": [message]}, status=status)
    messages.error(request, message)
    return redirect("dashboard")


def _recent_uploads(user, limit=50):
    uploads = list(
        Upload.objects.filter(user=user).prefetch_related(
            Prefetch("tracks", queryset=UploadRecord.objects.order_by("id"))
        )[:limit]
    )
    for upload in uploads:
        tracks = list(upload.tracks.all())
        upload.removed = sum(1 for track in tracks if track.status == UploadRecord.Status.REMOVED)
        prefix = f"{upload.name}/" if upload.kind == Upload.Kind.ARCHIVE else ""
        for track in tracks:
            name = track.original_name
            track.short_name = name[len(prefix):] if prefix and name.startswith(prefix) else name
        # A lone record that is the upload itself (single file, unreadable archive) needs no track list.
        upload.track_rows = tracks if len(tracks) > 1 or (tracks and tracks[0].short_name != upload.name) else []
    return uploads


def _dashboard_context(request):
    policy = get_policy()
    quota = quota_for(request.user, policy)
    used = used_quota(request.user)
    return {
        "uploads": _recent_uploads(request.user),
        "policy": policy,
        "quota": quota,
        "used": used,
        "used_percent": min(100, round(used * 100 / quota)) if quota else 0,
    }


def _js_strings():
    return {
        "uploading": _("Uploading files…"),
        "transferring": _("Transferring files: %(percent)s"),
        "processing": _("Processing on the server (extracting, checking for duplicates)…"),
        "done": _("Done."),
        "uploadError": _("Upload error."),
        "parseError": _("Could not process the server response."),
        "serverError": _("Server error (HTTP %(status)s)."),
        "tooLarge": _("The upload is too large for the server."),
        "sessionExpired": _("Your session has expired. Refresh the page and sign in again."),
        "networkError": _("Network error. Check your connection and refresh the page."),
        "cancelled": _("Upload cancelled."),
        "details": _("Details"),
        "scanStarted": _("Navidrome library scan started."),
        "status": {
            "success": _("Done"),
            "partial": _("Partly added"),
            "duplicate": _("Duplicate"),
            "failed": _("Failed"),
        },
    }


@login_required
def dashboard(request):
    context = _dashboard_context(request)
    context["js_strings"] = _js_strings()
    return render(request, "uploads/dashboard.html", context)


@login_required
def history(request):
    return render(request, "uploads/_history.html", _dashboard_context(request))


@login_required
@require_http_methods(["POST"])
def upload_files(request):
    policy = get_policy()
    files = request.FILES.getlist("files")
    if not files:
        return _reject(request, _("Select at least one file."), 400)
    if len(files) > policy.max_files_per_request:
        return _reject(
            request,
            _("At most %(max_files)s files can be uploaded in one request.") % {"max_files": policy.max_files_per_request},
            400,
        )
    if sum(uploaded_file.size for uploaded_file in files) > settings.MAX_UPLOAD_REQUEST_BYTES:
        return _reject(request, _("The total upload request exceeds the server limit."), 413)

    batch = UploadBatch(request.user, policy, password=request.POST.get("archive_password", ""))
    results = [process_upload(batch, uploaded_file) for uploaded_file in files]
    scan_started = batch.navidrome.start_scan() if batch.stored_count else False

    if _is_ajax(request):
        return JsonResponse(
            {
                "ok": all(item["status"] in ("success", "partial") for item in results),
                "results": results,
                "scan_started": scan_started,
            }
        )
    for result in results:
        level = {"success": messages.success, "partial": messages.warning, "duplicate": messages.warning}.get(result["status"], messages.error)
        level(request, f'{result["name"]}: {result["message"]}')
    return redirect("dashboard")
