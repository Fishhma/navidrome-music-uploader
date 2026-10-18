from django.conf import settings
from django.db import models
from django.utils import timezone


class UploadPolicy(models.Model):
    max_file_size = models.PositiveBigIntegerField(default=100 * 1024 * 1024, help_text="Maximum size of one uploaded file in bytes.")
    max_files_per_request = models.PositiveSmallIntegerField(default=20)
    daily_quota = models.PositiveBigIntegerField(default=1024 * 1024 * 1024, help_text="Default quota per user in a rolling 24-hour period, in bytes.")
    max_archive_entries = models.PositiveSmallIntegerField(default=100)
    max_archive_expanded_size = models.PositiveBigIntegerField(default=1024 * 1024 * 1024, help_text="Maximum total uncompressed size per archive, in bytes.")
    max_archive_ratio = models.PositiveSmallIntegerField(default=100, help_text="Maximum archive member expansion ratio.")

    class Meta:
        verbose_name = "upload policy"
        verbose_name_plural = "upload policy"

    def __str__(self):
        return "Global upload policy"


class UserUploadLimit(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="upload_limit")
    daily_quota = models.PositiveBigIntegerField(null=True, blank=True, help_text="Override the global rolling 24-hour quota; leave blank to use the global limit.")

    def __str__(self):
        return f"Upload limit for {self.user}"


class UploadRecord(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", "Completed"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="upload_records")
    username_snapshot = models.CharField(max_length=150)
    original_name = models.CharField(max_length=500)
    stored_name = models.CharField(max_length=500, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    message = models.CharField(max_length=300, blank=True)
    uploaded_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-uploaded_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["sha256"],
                condition=models.Q(status="success") & ~models.Q(sha256=""),
                name="unique_successful_upload_sha256",
            )
        ]

    def __str__(self):
        return f"{self.original_name} ({self.get_status_display()})"
