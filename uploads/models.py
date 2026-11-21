from collections import Counter

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class UploadPolicy(models.Model):
    max_file_size = models.PositiveBigIntegerField(default=1024 * 1024 * 1024, help_text="Maximum size of one uploaded file or archive in bytes.")
    max_files_per_request = models.PositiveSmallIntegerField(default=100, help_text="Maximum number of selected files per request.")
    daily_quota = models.PositiveBigIntegerField(default=1024 * 1024 * 1024, help_text="Default quota per user in a rolling 24-hour period, in bytes.")
    max_archive_entries = models.PositiveSmallIntegerField(default=300, help_text="Maximum number of file entries per archive, including non-media files. Directory entries are not counted.")
    max_archive_expanded_size = models.PositiveBigIntegerField(default=4 * 1024 * 1024 * 1024, help_text="Maximum total uncompressed size per archive, in bytes.")
    max_archive_ratio = models.PositiveSmallIntegerField(default=100, help_text="Maximum archive member expansion ratio.")
    check_tag_duplicates = models.BooleanField(
        default=True,
        help_text="Also treat a track as a duplicate when its artist, album and title tags match a track that is already in the library.",
    )

    class Meta:
        verbose_name = "upload policy"
        verbose_name_plural = "upload policy"

    def __str__(self):
        return "Global upload policy"


class UserUploadLimit(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="upload_limit")
    daily_quota = models.PositiveBigIntegerField(null=True, blank=True, help_text="Override the global rolling 24-hour quota; leave blank to use the global limit.")
    # Uploads before this moment do not count towards the rolling 24-hour quota (admin "reset" action).
    quota_reset_at = models.DateTimeField(null=True, blank=True, editable=False)

    def __str__(self):
        return f"Upload limit for {self.user}"


class Upload(models.Model):
    """One line of the upload history: an archive, or the loose tracks of one request that went to one folder."""

    class Kind(models.TextChoices):
        ARCHIVE = "archive", _("Archive")
        FOLDER = "folder", _("Folder")
        FILE = "file", _("File")

    class Status(models.TextChoices):
        SUCCESS = "success", _("Done")
        PARTIAL = "partial", _("Partly added")
        DUPLICATE = "duplicate", _("Duplicate")
        FAILED = "failed", _("Failed")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="uploads")
    username_snapshot = models.CharField(max_length=150)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    name = models.CharField(max_length=500)
    status = models.CharField(max_length=10, choices=Status.choices)
    # Reason shown when nothing was added (wrong password, all duplicates...).
    message = models.CharField(max_length=300, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    added = models.PositiveIntegerField(default=0)
    duplicates = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    # Library folders the tracks went to, relative to <library>, separated by newlines.
    folders = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-uploaded_at", "-id"]
        verbose_name = _("upload history entry")
        verbose_name_plural = _("upload history")

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"

    @property
    def folder_list(self):
        return [folder for folder in self.folders.splitlines() if folder]

    def refresh_summary(self, records):
        """Recompute counts, folders and status from the track records of this upload."""
        statuses = Counter(record.status for record in records)
        self.added = statuses[UploadRecord.Status.SUCCESS] + statuses[UploadRecord.Status.REMOVED]
        self.duplicates = statuses[UploadRecord.Status.DUPLICATE]
        self.failed = statuses[UploadRecord.Status.FAILED]
        folders = []
        for record in records:
            if record.stored_name and record.status == UploadRecord.Status.SUCCESS:
                folder = "/".join(["Uploads", *record.stored_name.split("/")[:-1]])
                if folder not in folders:
                    folders.append(folder)
        self.folders = "\n".join(folders)
        if self.added and not (self.duplicates or self.failed):
            self.status = self.Status.SUCCESS
        elif self.added:
            self.status = self.Status.PARTIAL
        elif self.duplicates and not self.failed:
            self.status = self.Status.DUPLICATE
        else:
            self.status = self.Status.FAILED


class UploadRecord(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", _("Done")
        DUPLICATE = "duplicate", _("Duplicate")
        FAILED = "failed", _("Failed")
        # A successful upload whose file has since disappeared from the library.
        REMOVED = "removed", _("Removed")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="upload_records")
    upload = models.ForeignKey(Upload, null=True, blank=True, on_delete=models.SET_NULL, related_name="tracks")
    username_snapshot = models.CharField(max_length=150)
    original_name = models.CharField(max_length=500)
    # Path relative to <library>/Uploads/.
    stored_name = models.CharField(max_length=500, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    artist = models.CharField(max_length=300, blank=True)
    album = models.CharField(max_length=300, blank=True)
    title = models.CharField(max_length=300, blank=True)
    # Normalized artist/album/title fingerprint used for tag-based duplicate detection.
    track_key = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    message = models.CharField(max_length=300, blank=True)
    uploaded_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-uploaded_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["sha256"],
                condition=models.Q(status="success") & ~models.Q(sha256=""),
                name="unique_successful_upload_sha256",
            )
        ]

    def __str__(self):
        return f"{self.original_name} ({self.get_status_display()})"

    @property
    def library_path(self):
        if not self.stored_name:
            return None
        return settings.UPLOAD_LIBRARY_DIR / "Uploads" / self.stored_name

    def file_exists(self):
        path = self.library_path
        return bool(path) and path.is_file()
