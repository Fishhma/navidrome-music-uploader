import io
import tempfile
import zipfile
import json
import pyzipper
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.assertEqual(record.original_name, "tracks.zip/folder/song.mp3")
        self.assertTrue((self.library / "Uploads" / record.stored_name).is_file())
        self.assertEqual(list((self.library / "Uploads").glob("*.txt")), [])


    @staticmethod
    def encrypted_zip(password="correct-horse"):
        contents = io.BytesIO()
        with pyzipper.AESZipFile(
            contents, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
        ) as archive:
            archive.setpassword(password.encode("utf-8"))
            archive.writestr("Album/track.mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00")
            archive.writestr("Album/cover.jpg", b"\xff\xd8\xff\xe0fake-jpeg-data")
            archive.writestr("Album/notes.txt", "ignored but counted")
        return contents.getvalue()

    def test_encrypted_aes_zip_prompts_for_password_and_keeps_cover_next_to_album(self):
        encrypted = self.encrypted_zip()
        response = self.post_files(self.user, SimpleUploadedFile("album.zip", encrypted))
        result = response.json()["results"][0]
        self.assertTrue(result["needs_password"])
        self.assertEqual(result["status"], "needs_password")
        self.assertEqual(UploadRecord.objects.count(), 0)

        self.client.force_login(self.user)
        response = self.client.post(
            "/upload/",
            {"files": [SimpleUploadedFile("album.zip", encrypted)], "archive_password_0": "correct-horse"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.json()["results"][0]["status"], "success")
        records = list(UploadRecord.objects.filter(status=UploadRecord.Status.SUCCESS))
        self.assertEqual(len(records), 2)
        cover = next(record for record in records if record.stored_name.lower().endswith("cover.jpg"))
        track = next(record for record in records if record.stored_name.lower().endswith(".mp3"))
        self.assertEqual(Path(cover.stored_name).parent, Path(track.stored_name).parent)
        self.assertTrue((self.library / "Uploads" / cover.stored_name).is_file())

    @unittest.skipUnless(shutil.which("zip"), "requires the system zip utility to create a ZipCrypto fixture")
    def test_zipcrypto_archive_password_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            track = root / "track.mp3"
            track.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00")
            archive_path = root / "zipcrypto.zip"
            subprocess.run(["zip", "-q", "-P", "correct-horse", str(archive_path), track.name],
                           cwd=root, check=True)
            encrypted = archive_path.read_bytes()
        self.client.force_login(self.user)
        response = self.client.post(
            "/upload/", {"files": [SimpleUploadedFile("zipcrypto.zip", encrypted)]},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(response.json()["results"][0]["needs_password"])
        self.client.force_login(self.user)
        response = self.client.post(
            "/upload/",
            {"files": [SimpleUploadedFile("zipcrypto.zip", encrypted)], "archive_password_0": "correct-horse"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.json()["results"][0]["status"], "success")

    def test_encrypted_zip_rejects_wrong_password_without_recording_secret(self):
        encrypted = self.encrypted_zip()
        self.client.force_login(self.user)
        response = self.client.post(
            "/upload/",
            {"files": [SimpleUploadedFile("album.zip", encrypted)], "archive_password_0": "wrong"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        result = response.json()["results"][0]
        self.assertTrue(result["needs_password"])
        self.assertTrue(result["password_invalid"])
        self.assertEqual(UploadRecord.objects.count(), 0)

    def test_folder_upload_preserves_nested_audio_and_cover_paths(self):
        files = [
            self.mp3("track.mp3"),
            SimpleUploadedFile("cover.jpg", b"\xff\xd8\xff\xe0fake-jpeg-data"),
            SimpleUploadedFile("notes.txt", b"ignored"),
        ]
        self.client.force_login(self.user)
        response = self.client.post(
            "/upload/",
            {
                "files": files,
                "folder_upload": "1",
                "relative_paths": json.dumps(["Album/Disc1/track.mp3", "Album/cover.jpg", "Album/notes.txt"]),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["status"] for r in response.json()["results"]], ["success", "success", "ignored"])
        records = list(UploadRecord.objects.filter(status=UploadRecord.Status.SUCCESS))
        self.assertEqual(len(records), 2)
        cover = next(record for record in records if record.stored_name.lower().endswith("cover.jpg"))
        track = next(record for record in records if record.stored_name.lower().endswith(".mp3"))
        self.assertNotEqual(Path(cover.stored_name).parent, Path(track.stored_name).parent)
        self.assertTrue((self.library / "Uploads" / cover.stored_name).is_file())

    def test_selected_file_limit_is_enforced_at_one_hundred(self):
        self.policy.max_files_per_request = 100
        self.policy.save()
        files = [self.mp3(f"track-{i}.mp3", b"ID3" + bytes([i % 256]) + b"\x00" * 6) for i in range(101)]
        with override_settings(DATA_UPLOAD_MAX_NUMBER_FILES=200):
            response = self.post_files(self.user, *files)
        self.assertEqual(response.status_code, 400)
        self.assertIn("100", response.json()["errors"][0])
        self.assertEqual(UploadRecord.objects.count(), 0)

    @patch("uploads.views.process_rar", return_value=1)
    def test_rar_extension_is_accepted_and_dispatched_to_rar_processor(self, process_rar_mock):
        response = self.post_files(self.user, SimpleUploadedFile("album.rar", b"RAR test fixture"))
        self.assertEqual(response.status_code, 200)
        process_rar_mock.assert_called_once()
        self.assertEqual(response.json()["results"][0]["status"], "success")

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

    def test_browser_language_is_detected(self):
        response = self.client.get("/accounts/login/", HTTP_ACCEPT_LANGUAGE="cs,en;q=0.8")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Language"), "cs")

    def test_language_selection_is_persisted_in_cookie(self):
        response = self.client.post("/i18n/setlang/", {"language": "en", "next": "/accounts/login/"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.cookies["django_language"].value, "en")

    def test_upload_requires_authentication(self):
        response = self.client.post("/upload/")
        self.assertRedirects(response, "/accounts/login/?next=/upload/", fetch_redirect_response=False)

    def test_custom_static_assets_are_discoverable_for_collectstatic(self):
        self.assertEqual(finders.find("css/app.css"), str(settings.BASE_DIR / "static" / "css" / "app.css"))
        self.assertEqual(finders.find("js/upload.js"), str(settings.BASE_DIR / "static" / "js" / "upload.js"))
