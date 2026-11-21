from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.template.defaultfilters import filesizeformat
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from .models import Upload, UploadPolicy, UploadRecord, UserUploadLimit
from .processing import get_policy, quota_for, reset_quota, used_quota

User = get_user_model()


def _report_reset(modeladmin, request, count):
    modeladmin.message_user(
        request,
        ngettext(
            "The 24-hour upload usage was reset for %(count)d user.",
            "The 24-hour upload usage was reset for %(count)d users.",
            count,
        ) % {"count": count},
        messages.SUCCESS,
    )


def _usage(user):
    return f"{filesizeformat(used_quota(user))} / {filesizeformat(quota_for(user, get_policy()))}"


@admin.register(UploadPolicy)
class UploadPolicyAdmin(admin.ModelAdmin):
    fields = (
        "max_file_size",
        "max_files_per_request",
        "daily_quota",
        "max_archive_entries",
        "max_archive_expanded_size",
        "max_archive_ratio",
        "check_tag_duplicates",
    )

    def has_add_permission(self, request):
        return not UploadPolicy.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UserUploadLimit)
class UserUploadLimitAdmin(admin.ModelAdmin):
    list_display = ("user", "daily_quota", "usage", "quota_reset_at")
    search_fields = ("user__username",)
    readonly_fields = ("usage", "quota_reset_at")
    actions = ["reset_usage"]

    @admin.display(description=_("Used in the last 24 hours"))
    def usage(self, obj):
        return _usage(obj.user) if obj.pk else "—"

    @admin.action(description=_("Reset the 24-hour upload usage"))
    def reset_usage(self, request, queryset):
        _report_reset(self, request, reset_quota(limit.user for limit in queryset.select_related("user")))


admin.site.unregister(User)


@admin.register(User)
class UploaderUserAdmin(UserAdmin):
    """The stock user admin plus the upload usage column and a reset action."""

    list_display = (*UserAdmin.list_display, "upload_usage")
    actions = [*(UserAdmin.actions or []), "reset_upload_usage"]

    @admin.display(description=_("Used in the last 24 hours"))
    def upload_usage(self, obj):
        return _usage(obj)

    @admin.action(description=_("Reset the 24-hour upload usage"))
    def reset_upload_usage(self, request, queryset):
        _report_reset(self, request, reset_quota(queryset))


class UploadRecordInline(admin.TabularInline):
    model = UploadRecord
    fields = ("original_name", "stored_name", "size", "status", "message")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Upload)
class UploadAdmin(admin.ModelAdmin):
    list_display = ("uploaded_at", "username_snapshot", "kind", "name", "size", "status", "added", "duplicates", "failed")
    list_filter = ("status", "kind", "uploaded_at")
    search_fields = ("username_snapshot", "name", "folders", "message")
    readonly_fields = (
        "user", "username_snapshot", "kind", "name", "status", "message", "size",
        "added", "duplicates", "failed", "folders", "uploaded_at",
    )
    inlines = [UploadRecordInline]
    ordering = ("-uploaded_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(UploadRecord)
class UploadRecordAdmin(admin.ModelAdmin):
    list_display = ("uploaded_at", "username_snapshot", "original_name", "size", "status", "message")
    list_filter = ("status", "uploaded_at")
    search_fields = ("username_snapshot", "original_name", "stored_name", "artist", "album", "title", "sha256")
    readonly_fields = (
        "user", "upload", "username_snapshot", "original_name", "stored_name", "size", "sha256",
        "artist", "album", "title", "track_key", "status", "message", "uploaded_at",
    )
    ordering = ("-uploaded_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
