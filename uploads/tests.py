import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .models import UploadPolicy, UploadRecord
from .navidrome import NavidromeClient

TEST_DATA = Path(__file__).resolve().parent / "test_data"
RAR_PASSWORD = "test-password"
HAS_UNRAR = shutil.which("unrar") is not None

try:
    from mutagen.easyid3 import EasyID3
except ImportError:  # pragma: no cover
    EasyID3 = None


def mp3_bytes(seed=b"x", artist=None, album=None, title=None):
    """A tiny but parseable MPEG-1 Layer III stream, optionally with ID3 tags."""
    frame = bytearray(bytes([0xFF, 0xFB, 0x90, 0x64]) + bytes(413))
    frame[4 : 4 + len(seed)] = seed
    data = bytes(frame) * 4
    if not (artist or album or title):
        return b"ID3\x04\x00\x00\x00\x00\x00\x00" + data
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
        handle.write(data)
        path = handle.name
    try:
        tags = EasyID3()
        if artist:
            tags["artist"] = artist
        if album:
            tags["album"] = album
        if title:
            tags["title"] = title
        tags.save(path)
        return Path(path).read_bytes()
    finally:
        Path(path).unlink()


def zip_bytes(entries):
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return contents.getvalue()


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
    NAVIDROME_URL="",
    NAVIDROME_USERNAME="",
    NAVIDROME_PASSWORD="",
)
class UploadTestCase(TestCase):
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

    @property
    def uploads(self):
        return self.library / "Uploads"

    def stored_files(self):
        if not self.uploads.exists():
            return []
        return sorted(p.relative_to(self.uploads).as_posix() for p in self.uploads.rglob("*") if p.is_file())

    def post_files(self, user, *files, password=None):
        self.client.force_login(user)
        # Assertions below check the English messages.
        self.client.cookies["django_language"] = "en"
        data = {"files": list(files)}
        if password is not None:
            data["archive_password"] = password
        response = self.client.post("/upload/", data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def post_one(self, *args, **kwargs):
        return self.post_files(*args, **kwargs)["results"][0]

    @staticmethod
    def upload(name, payload):
        return SimpleUploadedFile(name, payload, content_type="application/octet-stream")

    @staticmethod
    def fixture(name):
        return SimpleUploadedFile(name, (TEST_DATA / name).read_bytes(), content_type="application/octet-stream")


class SingleFileUploadTests(UploadTestCase):
    def test_uploads_signature_valid_audio_and_keeps_private_history(self):
        result = self.post_one(self.user, self.upload("track.mp3", mp3_bytes()))
        self.assertEqual(result["status"], "success")
        record = UploadRecord.objects.get(user=self.user)
        self.assertEqual(record.status, UploadRecord.Status.SUCCESS)
        self.assertEqual(record.stored_name, "track.mp3")
        self.assertTrue((self.uploads / "track.mp3").is_file())

        self.client.force_login(self.other)
        self.assertNotContains(self.client.get("/"), "track.mp3")

    def test_tagged_track_goes_to_album_folder(self):
        payload = mp3_bytes(b"a", artist="Orlik", album="Demise", title="Bila Liga")
        result = self.post_one(self.user, self.upload("01 Bila Liga.mp3", payload))
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.stored_files(), ["Orlik/Demise/01 Bila Liga.mp3"])
        record = UploadRecord.objects.get()
        self.assertEqual((record.artist, record.album, record.title), ("Orlik", "Demise", "Bila Liga"))
        self.assertTrue(record.track_key)

    def test_rejects_mismatched_content_and_records_failure(self):
        result = self.post_one(self.user, self.upload("fake.mp3", b"not audio"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(UploadRecord.objects.get().status, UploadRecord.Status.FAILED)
        self.assertEqual(self.stored_files(), [])

    def test_same_name_different_content_does_not_overwrite(self):
        self.post_one(self.user, self.upload("track.mp3", mp3_bytes(b"one")))
        result = self.post_one(self.user, self.upload("track.mp3", mp3_bytes(b"two")))
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.stored_files(), ["track (2).mp3", "track.mp3"])


class DuplicateDetectionTests(UploadTestCase):
    def test_exact_duplicate_is_reported_as_duplicate(self):
        self.post_one(self.user, self.upload("track.mp3", mp3_bytes()))
        result = self.post_one(self.other, self.upload("different-name.mp3", mp3_bytes()))
        self.assertEqual(result["status"], "duplicate")
        self.assertIn("track.mp3", result["message"])
        self.assertEqual(UploadRecord.objects.filter(status=UploadRecord.Status.SUCCESS).count(), 1)
        self.assertEqual(UploadRecord.objects.filter(status=UploadRecord.Status.DUPLICATE).count(), 1)
        self.assertEqual(self.stored_files(), ["track.mp3"])

    def test_duplicate_within_one_request(self):
        response = self.post_files(self.user, self.upload("a.mp3", mp3_bytes()), self.upload("b.mp3", mp3_bytes()))
        self.assertEqual([r["status"] for r in response["results"]], ["success", "duplicate"])
        self.assertEqual(self.stored_files(), ["a.mp3"])

    def test_file_removed_from_library_can_be_uploaded_again(self):
        self.post_one(self.user, self.upload("track.mp3", mp3_bytes()))
        (self.uploads / "track.mp3").unlink()
        result = self.post_one(self.user, self.upload("track.mp3", mp3_bytes()))
        self.assertEqual(result["status"], "success")
        self.assertEqual(UploadRecord.objects.filter(status=UploadRecord.Status.REMOVED).count(), 1)
        self.assertEqual(UploadRecord.objects.filter(status=UploadRecord.Status.SUCCESS).count(), 1)
        self.assertEqual(self.stored_files(), ["track.mp3"])

    def test_same_tags_with_different_content_is_duplicate(self):
        self.post_one(self.user, self.upload("song.mp3", mp3_bytes(b"128k", artist="Band", album="Album", title="Song")))
        result = self.post_one(self.user, self.upload("song-v2.mp3", mp3_bytes(b"320k", artist="BAND", album="album", title="Song!")))
        self.assertEqual(result["status"], "duplicate")
        self.assertIn("song.mp3", result["message"])

    def test_tag_check_can_be_disabled(self):
        self.policy.check_tag_duplicates = False
        self.policy.save()
        self.post_one(self.user, self.upload("song.mp3", mp3_bytes(b"128k", artist="Band", album="Album", title="Song")))
        result = self.post_one(self.user, self.upload("song-v2.mp3", mp3_bytes(b"320k", artist="Band", album="Album", title="Song")))
        self.assertEqual(result["status"], "success")

    def test_same_title_on_different_album_is_not_duplicate(self):
        self.post_one(self.user, self.upload("song.mp3", mp3_bytes(b"1", artist="Band", album="Studio", title="Song")))
        result = self.post_one(self.user, self.upload("song-live.mp3", mp3_bytes(b"2", artist="Band", album="Live", title="Song")))
        self.assertEqual(result["status"], "success")

    def test_navidrome_library_duplicate(self):
        song = {"title": "Song", "artist": "Band", "album": "Album", "path": "Band/Album/01 Song.flac"}
        with patch.object(NavidromeClient, "enabled", True), patch.object(NavidromeClient, "_call", return_value={"searchResult3": {"song": [song]}}) as call:
            result = self.post_one(self.user, self.upload("song.mp3", mp3_bytes(b"1", artist="Band", album="Album", title="Song")))
        self.assertEqual(result["status"], "duplicate")
        self.assertIn("Band/Album/01 Song.flac", result["message"])
        self.assertEqual(call.call_args.args[0], "search3")
        self.assertEqual(self.stored_files(), [])

    def test_navidrome_outage_does_not_block_uploads(self):
        with patch.object(NavidromeClient, "enabled", True), patch.object(NavidromeClient, "_call", side_effect=OSError("down")):
            result = self.post_one(self.user, self.upload("song.mp3", mp3_bytes(b"1", artist="Band", album="Album", title="Song")))
        self.assertEqual(result["status"], "success")

    @override_settings(NAVIDROME_URL="http://navidrome:4533", NAVIDROME_USERNAME="admin", NAVIDROME_PASSWORD="secret")
    def test_navidrome_scan_starts_after_successful_upload(self):
        calls = []

        def fake_call(self_, endpoint, **params):
            calls.append(endpoint)
            return {"searchResult3": {}}

        with patch.object(NavidromeClient, "_call", fake_call):
            response = self.post_files(self.user, self.upload("track.mp3", mp3_bytes()))
            self.assertTrue(response["scan_started"])
            response = self.post_files(self.user, self.upload("track.mp3", mp3_bytes()))
            self.assertFalse(response["scan_started"])
        self.assertEqual(calls.count("startScan"), 1)

    def test_enforces_daily_quota(self):
        self.policy.daily_quota = 4
        self.policy.save()
        result = self.post_one(self.user, self.upload("track.mp3", mp3_bytes()))
        self.assertEqual(result["status"], "failed")
        self.assertIn("quota", result["message"])


class ZipUploadTests(UploadTestCase):
    def test_zip_keeps_album_folder_and_cover(self):
        payload = zip_bytes({
            "My Album/01 song.mp3": mp3_bytes(b"1"),
            "My Album/cover.jpg": b"\xff\xd8\xff\xe0" + bytes(100),
            "My Album/notes.txt": "not extracted",
        })
        result = self.post_one(self.user, self.upload("tracks.zip", payload))
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.stored_files(), ["Unknown Artist/My Album/01 song.mp3", "Unknown Artist/My Album/cover.jpg"])
        record = UploadRecord.objects.get()
        self.assertEqual(record.original_name, "tracks.zip/My Album/01 song.mp3")
        self.assertEqual(record.stored_name, "Unknown Artist/My Album/01 song.mp3")

    def test_zip_without_folder_uses_archive_name(self):
        result = self.post_one(self.user, self.upload("Best Of.zip", zip_bytes({"song.mp3": mp3_bytes()})))
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.stored_files(), ["Unknown Artist/Best Of/song.mp3"])

    def test_zip_skips_duplicates_and_invalid_files_but_adds_the_rest(self):
        self.post_one(self.user, self.upload("old.mp3", mp3_bytes(b"old")))
        payload = zip_bytes({
            "Album/01 old.mp3": mp3_bytes(b"old"),
            "Album/02 new.mp3": mp3_bytes(b"new"),
            "Album/03 copy of new.mp3": mp3_bytes(b"new"),
            "Album/04 bad.mp3": b"not audio",
        })
        result = self.post_one(self.user, self.upload("album.zip", payload))
        self.assertEqual(result["status"], "partial", result)
        statuses = sorted(d["status"] for d in result["details"])
        self.assertEqual(statuses, ["duplicate", "duplicate", "failed"])
        self.assertEqual(self.stored_files(), ["Unknown Artist/Album/02 new.mp3", "old.mp3"])
        self.assertEqual(UploadRecord.objects.filter(status=UploadRecord.Status.SUCCESS).count(), 2)

    def test_reuploading_same_zip_is_duplicate(self):
        payload = zip_bytes({"Album/01.mp3": mp3_bytes(b"1"), "Album/02.mp3": mp3_bytes(b"2")})
        self.post_one(self.user, self.upload("album.zip", payload))
        result = self.post_one(self.user, self.upload("album.zip", payload))
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(len(self.stored_files()), 2)

    def test_windows_cp866_names_are_decoded(self):
        class LegacyZipInfo(zipfile.ZipInfo):
            # Simulate a Russian Windows archiver: cp866 bytes without the UTF-8 flag.
            def _encodeFilenameFlags(self):
                return self.filename.encode("cp866"), self.flag_bits & ~0x800

        contents = io.BytesIO()
        with zipfile.ZipFile(contents, "w") as archive:
            archive.writestr(LegacyZipInfo("Кино/Группа крови.mp3"), mp3_bytes())
        result = self.post_one(self.user, self.upload("kino.zip", contents.getvalue()))
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.stored_files(), ["Unknown Artist/Кино/Группа крови.mp3"])

    def test_untagged_artist_album_folder_name_is_split(self):
        result = self.post_one(self.user, self.upload("a.zip", zip_bytes({"Orlik - Demise/01.mp3": mp3_bytes()})))
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.stored_files(), ["Orlik/Demise/01.mp3"])

    def test_tags_decide_artist_and_album_folders(self):
        payload = zip_bytes({
            "rip/CD1/01.mp3": mp3_bytes(b"1", artist="Band", album="Double", title="One"),
            "rip/CD2/01.mp3": mp3_bytes(b"2", artist="Band", album="Double", title="Two"),
            "rip/folder.jpg": b"\xff\xd8\xff\xe0" + bytes(10),
            "Mix/01.mp3": mp3_bytes(b"3", artist="A", album="Mix", title="X"),
            "Mix/02.mp3": mp3_bytes(b"4", artist="B", album="Mix", title="Y"),
        })
        result = self.post_one(self.user, self.upload("a.zip", payload))
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.stored_files(), [
            "Band/Double/CD1/01.mp3",
            "Band/Double/CD2/01.mp3",
            "Band/Double/folder.jpg",
            "Various Artists/Mix/01.mp3",
            "Various Artists/Mix/02.mp3",
        ])

    def test_rejects_traversal_zip_without_writing_files(self):
        result = self.post_one(self.user, self.upload("unsafe.zip", zip_bytes({"../outside.mp3": mp3_bytes()})))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(list(self.library.rglob("*")), [])

    def test_zip_with_only_invalid_audio_fails(self):
        result = self.post_one(self.user, self.upload("bad.zip", zip_bytes({"bad.mp3": b"not audio"})))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.stored_files(), [])

    def test_zip_without_audio_fails(self):
        result = self.post_one(self.user, self.upload("docs.zip", zip_bytes({"readme.txt": "hi"})))
        self.assertEqual(result["status"], "failed")
        self.assertIn("no supported audio", result["message"])

    def test_quota_failure_rolls_back_whole_archive(self):
        self.policy.daily_quota = 2000
        self.policy.save()
        payload = zip_bytes({"Album/01.mp3": mp3_bytes(b"1"), "Album/02.mp3": mp3_bytes(b"2")})
        result = self.post_one(self.user, self.upload("album.zip", payload))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.stored_files(), [])


@unittest.skipUnless(HAS_UNRAR, "the official unrar binary is not installed")
class RarUploadTests(UploadTestCase):
    def setUp(self):
        super().setUp()
        # The fixtures contain tiny, highly compressible MP3 frames.
        self.policy.max_archive_ratio = 1000
        self.policy.save()

    def test_encrypted_stored_rar4_album(self):
        # Same layout as real-world albums that failed with unar: RAR 3.x, -m0, encrypted files.
        result = self.post_one(self.user, self.fixture("album_rar4_encrypted_stored.rar"), password=RAR_PASSWORD)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(
            self.stored_files(),
            ["Test Artist/Test Album/01. First.mp3", "Test Artist/Test Album/02. Second.mp3", "Test Artist/Test Album/cover.jpg"],
        )
        first = UploadRecord.objects.get(title="First")
        self.assertEqual(first.artist, "Test Artist")
        self.assertEqual(first.original_name, "album_rar4_encrypted_stored.rar/Test Artist - Test Album/01. First.mp3")

    def test_password_is_requested_and_never_echoed(self):
        result = self.post_one(self.user, self.fixture("album_rar4_encrypted_stored.rar"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("password", result["message"].lower())
        result = self.post_one(self.user, self.fixture("album_rar4_encrypted_stored.rar"), password="wrong-password")
        self.assertEqual(result["status"], "failed")
        self.assertIn("incorrect", result["message"])
        self.assertNotIn("wrong-password", str(result))
        self.assertFalse(UploadRecord.objects.filter(message__contains="wrong-password").exists())
        self.assertEqual(self.stored_files(), [])

    def test_rar5_with_encrypted_headers(self):
        result = self.post_one(self.user, self.fixture("album_rar5_header_encrypted.rar"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("password", result["message"].lower())
        result = self.post_one(self.user, self.fixture("album_rar5_header_encrypted.rar"), password="wrong-password")
        self.assertEqual(result["status"], "failed")
        result = self.post_one(self.user, self.fixture("album_rar5_header_encrypted.rar"), password=RAR_PASSWORD)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(len(self.stored_files()), 3)

    def test_same_album_in_different_archive_format_is_duplicate(self):
        self.post_one(self.user, self.fixture("album_rar4_encrypted_stored.rar"), password=RAR_PASSWORD)
        result = self.post_one(self.user, self.fixture("album_rar5_header_encrypted.rar"), password=RAR_PASSWORD)
        self.assertEqual(result["status"], "duplicate", result)
        self.assertEqual(len(result["details"]), 2)
        self.assertEqual(len(self.stored_files()), 3)

    def test_solid_rar_with_duplicate_inside(self):
        result = self.post_one(self.user, self.fixture("dupes_rar5_solid.rar"))
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual(
            self.stored_files(),
            ["Other Artist/Live/CD1/01. Opening.mp3", "Other Artist/Live/CD2/02. Closing.mp3"],
        )
        self.assertEqual([d["status"] for d in result["details"]], ["duplicate"])


class ViewTests(UploadTestCase):
    def test_archive_password_is_not_returned(self):
        payload = zip_bytes({"song.mp3": mp3_bytes()})
        self.client.force_login(self.user)
        response = self.client.post(
            "/upload/",
            {"files": [self.upload("a.zip", payload)], "archive_password": "secret-pass"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertNotIn("secret-pass", response.content.decode())

    def test_no_files_selected(self):
        self.client.force_login(self.user)
        response = self.client.post("/upload/", {}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 400)

    def test_history_fragment(self):
        self.post_one(self.user, self.upload("track.mp3", mp3_bytes()))
        response = self.client.get("/history/")
        self.assertContains(response, "track.mp3")
        self.assertContains(response, 'id="quota-card"')

    def test_dashboard_translations(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get("/", HTTP_ACCEPT_LANGUAGE="ru"), "Загрузить музыку")
        self.client.cookies["django_language"] = "en"
        self.assertContains(self.client.get("/"), "Upload music")
        self.client.cookies["django_language"] = "cs"
        self.assertContains(self.client.get("/"), "Nahrát hudbu")

    def test_anonymous_users_must_log_in(self):
        self.client.logout()
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/history/").status_code, 302)


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
