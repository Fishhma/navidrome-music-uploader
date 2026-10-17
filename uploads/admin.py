from django.contrib import admin

from .models import UploadPolicy, UploadRecord, UserUploadLimit


@admin.register(UploadPolicy)
class UploadPolicyAdmin(admin.ModelAdmin):
    fields = (
        "max_file_size",
        "max_files_per_request",
        "daily_quota",
        "max_archive_entries",
        "max_archive_expanded_size",
        "max_archive_ratio",
    )

    def has_add_permission(self, request):
        return not UploadPolicy.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UserUploadLimit)
class UserUploadLimitAdmin(admin.ModelAdmin):
    list_display = ("user", "daily_quota")
    search_fields = ("user__username",)


@admin.register(UploadRecord)
class UploadRecordAdmin(admin.ModelAdmin):
    list_display = ("uploaded_at", "username_snapshot", "original_name", "size", "status", "message")
    list_filter = ("status", "uploaded_at")
    search_fields = ("username_snapshot", "original_name", "sha256")
    readonly_fields = ("user", "username_snapshot", "original_name", "stored_name", "size", "sha256", "status", "message", "uploaded_at")
    ordering = ("-uploaded_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
