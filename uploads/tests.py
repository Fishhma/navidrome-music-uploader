import io
import tempfile
import zipfile
from pathlib import Path

from django.contrib.auth.models import User
from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .models import UploadPolicy, UploadRecord


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class UploadFlowTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.incoming = root / "incoming"
        self.library = root / "music"
        self.incoming.mkdir()
        self.library.mkdir()
        self.settings_override = override_settings(UPLOAD_INCOMING_DIR=self.incoming, UPLOAD_LIBRARY_DIR=self.library)
        self.settings_override.enable()
        self.user = User.objects.create_user(username="listener", password="a-long-enough-password")
        self.other = User.objects.create_user(username="other", password="another-long-password")
        self.policy = UploadPolicy.objects.create(pk=1)

    def tearDown(self):
        self.settings_override.disable()
        self.temp.cleanup()

    @staticmethod
    def mp3(name="track.mp3", payload=b"ID3\x04\x00\x00\x00\x00\x00\x00"):
        return SimpleUploadedFile(name, payload, content_type="application/octet-stream")

    def post_files(self, user, *files):
        self.client.force_login(user)
        return self.client.post(
            "/upload/",
            {"files": list(files)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def test_uploads_signature_valid_audio_and_keeps_private_history(self):
        response = self.post_files(self.user, self.mp3())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["status"], "success")
        record = UploadRecord.objects.get(user=self.user)
        self.assertEqual(record.status, UploadRecord.Status.SUCCESS)
        self.assertTrue((self.library / "Uploads" / record.stored_name).is_file())

        self.client.force_login(self.other)
        response = self.client.get("/")
        self.assertNotContains(response, "track.mp3")

    def test_rejects_mismatched_content_and_records_failure(self):
        response = self.post_files(self.user, SimpleUploadedFile("fake.mp3", b"not audio"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["status"], "failed")
        self.assertEqual(UploadRecord.objects.get().status, UploadRecord.Status.FAILED)

    def test_rejects_duplicate_content(self):
        self.post_files(self.user, self.mp3())
        response = self.post_files(self.other, self.mp3("different-name.mp3"))
        self.assertEqual(response.json()["results"][0]["status"], "failed")
        self.assertEqual(UploadRecord.objects.filter(status=UploadRecord.Status.SUCCESS).count(), 1)

    def test_enforces_daily_quota(self):
        self.policy.daily_quota = 4
        self.policy.save()
        response = self.post_files(self.user, self.mp3(payload=b"ID3\x00\x00\x00\x00\x00\x00\x01"))
        self.assertEqual(response.json()["results"][0]["status"], "failed")
        self.assertIn("quota", response.json()["results"][0]["message"])

    def test_safe_zip_extracts_audio_without_paths(self):
        contents = io.BytesIO()
        with zipfile.ZipFile(contents, "w") as archive:
            archive.writestr("folder/song.mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00")
            archive.writestr("notes.txt", "not extracted")
        response = self.post_files(self.user, SimpleUploadedFile("tracks.zip", contents.getvalue()))
        self.assertEqual(response.json()["results"][0]["status"], "success")
        record = UploadRecord.objects.get()
        self.assertEqual(record.original_name, "tracks.zip/song.mp3")
        self.assertTrue((self.library / "Uploads" / record.stored_name).is_file())
        self.assertEqual(list((self.library / "Uploads").glob("*.txt")), [])

    def test_rejects_traversal_zip_without_writing_files(self):
        contents = io.BytesIO()
        with zipfile.ZipFile(contents, "w") as archive:
            archive.writestr("../outside.mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00")
        response = self.post_files(self.user, SimpleUploadedFile("unsafe.zip", contents.getvalue()))
        self.assertEqual(response.json()["results"][0]["status"], "failed")
        self.assertEqual(list(self.library.rglob("*")), [])

    def test_zip_validation_is_all_or_nothing(self):
        contents = io.BytesIO()
        with zipfile.ZipFile(contents, "w") as archive:
            archive.writestr("good.mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00")
            archive.writestr("bad.mp3", b"not audio")
        response = self.post_files(self.user, SimpleUploadedFile("mixed.zip", contents.getvalue()))
        self.assertEqual(response.json()["results"][0]["status"], "failed")
        self.assertEqual(UploadRecord.objects.count(), 1)
        self.assertFalse((self.library / "Uploads").exists())

    def test_anonymous_users_must_log_in(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)


class UploadPolicyValidationTests(TestCase):
    def test_policy_singleton_is_admin_managed(self):
        self.assertTrue(UploadPolicy._meta.get_field("max_file_size").default > 0)


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class LoginAndRouteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="listener", password="a-long-enough-password")
        UploadPolicy.objects.create(pk=1)

    def test_home_redirects_to_login_with_return_target(self):
        response = self.client.get("/")
        self.assertRedirects(response, "/accounts/login/?next=/", fetch_redirect_response=False)

    def test_login_page_renders_form_and_csrf_token(self):
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "С возвращением")
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')

    def test_successful_login_returns_to_protected_home(self):
        response = self.client.post(
            "/accounts/login/",
            {"username": "listener", "password": "a-long-enough-password"},
        )
        self.assertRedirects(response, "/")
        self.assertContains(self.client.get("/"), "Загрузить музыку")

    def test_invalid_login_renders_login_error(self):
        response = self.client.post(
            "/accounts/login/",
            {"username": "listener", "password": "incorrect-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Пожалуйста, введите правильные имя пользователя и пароль")

    def test_upload_requires_authentication(self):
        response = self.client.post("/upload/")
        self.assertRedirects(response, "/accounts/login/?next=/upload/", fetch_redirect_response=False)

    def test_custom_static_assets_are_discoverable_for_collectstatic(self):
        self.assertEqual(finders.find("css/app.css"), str(settings.BASE_DIR / "static" / "css" / "app.css"))
        self.assertEqual(finders.find("js/upload.js"), str(settings.BASE_DIR / "static" / "js" / "upload.js"))
