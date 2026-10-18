from django.db import migrations, models


def raise_existing_defaults(apps, schema_editor):
    UploadPolicy = apps.get_model("uploads", "UploadPolicy")
    for policy in UploadPolicy.objects.all():
        changed = []
        # Raise upload limits to the requested floors, preserve larger values, and leave quotas untouched.
        if policy.max_files_per_request < 100:
            policy.max_files_per_request = 100
            changed.append("max_files_per_request")
        if policy.max_file_size < 1024 * 1024 * 1024:
            policy.max_file_size = 1024 * 1024 * 1024
            changed.append("max_file_size")
        if policy.max_archive_expanded_size < 4 * 1024 * 1024 * 1024:
            policy.max_archive_expanded_size = 4 * 1024 * 1024 * 1024
            changed.append("max_archive_expanded_size")
        if policy.max_archive_entries < 100:
            policy.max_archive_entries = 100
            changed.append("max_archive_entries")
        if changed:
            policy.save(update_fields=changed)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("uploads", "0001_initial")]
    operations = [
        migrations.AlterField(
            model_name="uploadpolicy",
            name="max_file_size",
            field=models.PositiveBigIntegerField(default=1073741824, help_text="Maximum size of one uploaded file or archive in bytes."),
        ),
        migrations.AlterField(
            model_name="uploadpolicy",
            name="max_files_per_request",
            field=models.PositiveSmallIntegerField(default=100, help_text="Maximum number of selected files per request or file entries per archive."),
        ),
        migrations.AlterField(
            model_name="uploadpolicy",
            name="max_archive_expanded_size",
            field=models.PositiveBigIntegerField(default=4294967296, help_text="Maximum total uncompressed size per archive, in bytes."),
        ),
        migrations.AlterField(
            model_name="uploadpolicy",
            name="max_archive_ratio",
            field=models.PositiveSmallIntegerField(default=100, help_text="Maximum archive member expansion ratio."),
        ),
        migrations.AlterField(
            model_name="uploadpolicy",
            name="max_archive_entries",
            field=models.PositiveSmallIntegerField(default=100, help_text="Maximum number of file entries per archive, including non-media files. Directory entries are not counted."),
        ),
        migrations.RunPython(raise_existing_defaults, noop_reverse),
    ]
