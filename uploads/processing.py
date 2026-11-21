"""Upload pipeline: validate, de-duplicate and store audio files and archives."""

import hashlib
import logging
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from .archives import extract_rar, extract_zip
from .library import LibraryWriter
from .models import UploadPolicy, UploadRecord, UserUploadLimit
from .navidrome import NavidromeClient
from .tags import TrackTags, read_tags
from .validators import (
    MAX_FOLDER_DEPTH,
    MAX_IMAGE_SIZE,
    UploadRejected,
    incoming_directory,
    inspect_audio,
    is_rar_name,
    is_zip_name,
    looks_like_image,
    safe_basename,
    safe_folder_name,
)

logger = logging.getLogger(__name__)
HASH_CHUNK = 1024 * 1024


def get_policy():
    policy, _created = UploadPolicy.objects.get_or_create(pk=1)
    return policy


def quota_for(user, policy):
    try:
        override = user.upload_limit.daily_quota
    except UserUploadLimit.DoesNotExist:
        override = None
    return override if override is not None else policy.daily_quota


def used_quota(user):
    since = timezone.now() - timedelta(hours=24)
    return (
        UploadRecord.objects.filter(user=user, status=UploadRecord.Status.SUCCESS, uploaded_at__gte=since)
        .aggregate(total=Sum("size"))["total"]
        or 0
    )


def sha256_of(source):
    digest = hashlib.sha256()
    if isinstance(source, (str, os.PathLike)):
        with open(source, "rb") as handle:
            for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
                digest.update(chunk)
    else:
        source.seek(0)
        for chunk in iter(lambda: source.read(HASH_CHUNK), b""):
            digest.update(chunk)
        source.seek(0)
    return digest.hexdigest()


@dataclass
class Track:
    original_name: str  # Shown in history, e.g. "Album.rar/01. Song.mp3"
    filename: str  # Sanitized file name inside the library
    source: object  # Path or binary stream
    size: int
    sha256: str = ""
    tags: TrackTags = field(default_factory=TrackTags)
    rel_dir: tuple = ()
    stored_name: str = ""


class UploadBatch:
    """State shared by all files of one upload request (duplicate checks across files, Navidrome)."""

    def __init__(self, user, policy, password=None, navidrome=None):
        self.user = user
        self.policy = policy
        self.password = password or None
        self.navidrome = navidrome if navidrome is not None else NavidromeClient()
        self.seen_hashes = {}
        self.seen_keys = {}
        self.stored_count = 0

    # ------------------------------------------------------------------ duplicate detection

    def _live_record(self, queryset):
        """Return the first successful record whose file still exists; retire records whose file is gone."""
        for record in queryset.filter(status=UploadRecord.Status.SUCCESS).order_by("uploaded_at"):
            if record.file_exists():
                return record
            record.status = UploadRecord.Status.REMOVED
            record.message = _("The file is no longer in the library.")[:300]
            record.save(update_fields=["status", "message"])
        return None

    def find_duplicate(self, track):
        """Return a human-readable reason if the track is already in the library, else None."""
        if track.sha256 in self.seen_hashes:
            return _("Same file as %(name)s in this upload.") % {"name": self.seen_hashes[track.sha256]}
        record = self._live_record(UploadRecord.objects.filter(sha256=track.sha256))
        if record:
            return _("Already uploaded on %(date)s by %(user)s as %(name)s.") % {
                "date": date_format(timezone.localtime(record.uploaded_at), "SHORT_DATE_FORMAT"),
                "user": record.username_snapshot,
                "name": record.original_name,
            }
        if not self.policy.check_tag_duplicates:
            return None
        key = track.tags.key
        if not key:
            return None
        if key in self.seen_keys:
            return _("Same track as %(name)s in this upload.") % {"name": self.seen_keys[key]}
        record = self._live_record(UploadRecord.objects.filter(track_key=key))
        if record:
            return _("This track (%(track)s) is already in the library: %(name)s.") % {
                "track": track.tags.describe(),
                "name": record.original_name,
            }
        song = self.navidrome.find_song(track.tags.artist, track.tags.album, track.tags.title)
        if song:
            return _("This track (%(track)s) is already in the Navidrome library: %(path)s.") % {
                "track": track.tags.describe(),
                "path": song.get("path") or song.get("title", ""),
            }
        return None

    def remember(self, track):
        self.seen_hashes[track.sha256] = track.original_name
        if self.policy.check_tag_duplicates and track.tags.key:
            self.seen_keys[track.tags.key] = track.original_name

    # ------------------------------------------------------------------ records

    def _record(self, status, original_name, message, size=0, track=None):
        tags = track.tags if track else TrackTags()
        return UploadRecord.objects.create(
            user=self.user,
            username_snapshot=self.user.get_username(),
            original_name=original_name[:500],
            stored_name=(track.stored_name if track else "")[:500],
            size=max(0, size),
            sha256=track.sha256 if track else "",
            artist=tags.artist[:300],
            album=tags.album[:300],
            title=tags.title[:300],
            track_key=tags.key if track else "",
            status=status,
            message=message[:300],
        )

    def record_failure(self, original_name, message, size=0):
        self._record(UploadRecord.Status.FAILED, original_name, message, size)

    def record_duplicate(self, track, reason):
        self._record(UploadRecord.Status.DUPLICATE, track.original_name, reason, track.size, track)

    # ------------------------------------------------------------------ storing

    def check_quota(self, total_size):
        if used_quota(self.user) + total_size > quota_for(self.user, self.policy):
            raise UploadRejected(_("The rolling 24-hour upload quota would be exceeded."))

    def store_tracks(self, tracks, images=(), success_message=""):
        """Write tracks (and optional cover images) and create their records atomically."""
        writer = LibraryWriter()
        try:
            for track in tracks:
                track.stored_name = writer.store(track.source, track.rel_dir, track.filename)
            for rel_dir, filename, path in images:
                writer.store(path, rel_dir, filename, skip_if_exists=True)
            with transaction.atomic():
                for track in tracks:
                    self._record(UploadRecord.Status.SUCCESS, track.original_name, success_message, track.size, track)
        except IntegrityError as exc:
            writer.rollback()
            # Another upload of the same file finished a moment earlier.
            raise UploadRejected(_("This audio file has already been uploaded.")) from exc
        except BaseException:
            writer.rollback()
            raise
        self.stored_count += len(tracks)
        for track in tracks:
            self.remember(track)


UNKNOWN_ARTIST = "Unknown Artist"
VARIOUS_ARTISTS = "Various Artists"


def library_dir(artist, album):
    """Uploads/<Artist>/<Album> (either part may be missing)."""
    return tuple(safe_folder_name(part) for part in (artist, album) if part)


def single_track_dir(tags):
    if tags.album_artist and tags.album:
        return library_dir(tags.album_artist, tags.album)
    if tags.album_artist:
        return library_dir(tags.album_artist, "")
    return ()


def prepare_track(batch, track):
    """Signature check, size check, hashing and tag reading. Raises UploadRejected for invalid audio."""
    with open(track.source, "rb") as stream:
        inspect_audio(stream, track.filename)
    if track.size > batch.policy.max_file_size:
        raise UploadRejected(_("The file exceeds the configured per-file size limit."))
    track.sha256 = sha256_of(track.source)
    track.tags = read_tags(track.source)


def process_single_audio(batch, uploaded_file, name):
    temporary_path = getattr(uploaded_file, "temporary_file_path", None)
    path = Path(temporary_path()) if temporary_path else None
    track = Track(original_name=name, filename=safe_basename(name), source=path, size=uploaded_file.size)
    if path is None:
        # Small in-memory upload: spill to disk so mutagen can read the tags.
        with tempfile.NamedTemporaryFile(dir=incoming_directory(), suffix=Path(track.filename).suffix, delete=False) as spill:
            uploaded_file.seek(0)
            for chunk in uploaded_file.chunks():
                spill.write(chunk)
            spill_path = Path(spill.name)
        try:
            track.source = spill_path
            return _process_single_track(batch, track)
        finally:
            spill_path.unlink(missing_ok=True)
    return _process_single_track(batch, track)


def _process_single_track(batch, track):
    prepare_track(batch, track)
    reason = batch.find_duplicate(track)
    if reason:
        batch.record_duplicate(track, reason)
        batch.remember(track)
        return {"name": track.original_name, "status": "duplicate", "message": reason}
    batch.check_quota(track.size)
    track.rel_dir = single_track_dir(track.tags)
    batch.store_tracks([track], success_message=_("Added to the shared music library."))
    return {
        "name": track.original_name,
        "status": "success",
        "message": _("Added to the library as %(path)s.") % {"path": f"Uploads/{track.stored_name}"},
    }


def _guess_from_folder(name):
    """Folder names like "Artist - Album" are the usual fallback for untagged files."""
    if " - " in name:
        artist, album = name.split(" - ", 1)
        return artist.strip(), album.strip()
    return "", name.strip()


def _common_prefix(paths):
    paths = list(paths)
    common = list(paths[0]) if paths else []
    for parts in paths[1:]:
        while common and list(parts[: len(common)]) != common:
            common.pop()
    return tuple(common)


class ArchiveLayout:
    """Maps archive folders to Uploads/<Artist>/<Album>[/<disc folder>] using the audio tags.

    The artist is the album artist (or the track artist; several different artists in one folder
    become "Various Artists"). Untagged files fall back to an "Artist - Album" folder name. Folders
    that end up in the same album (CD1/CD2...) keep their sub-folder so file names do not collide.
    """

    def __init__(self, tracks_by_dir, archive_name):
        self.archive_stem = Path(archive_name).stem
        self.targets = {source_dir: self._target(source_dir, tracks) for source_dir, tracks in tracks_by_dir.items()}
        self.dirs = {}
        by_target = {}
        for source_dir, target in self.targets.items():
            by_target.setdefault(target, []).append(source_dir)
        for target, source_dirs in by_target.items():
            prefix = _common_prefix(source_dirs) if len(source_dirs) > 1 else tuple(source_dirs[0])
            for source_dir in source_dirs:
                sub = tuple(safe_folder_name(part) for part in source_dir[len(prefix):])
                self.dirs[source_dir] = (library_dir(*target) + sub)[:MAX_FOLDER_DEPTH]

    def _target(self, source_dir, tracks):
        albums = Counter(t.tags.album for t in tracks if t.tags.album)
        album = albums.most_common(1)[0][0] if albums else ""
        album_artists = {t.tags.albumartist for t in tracks if t.tags.albumartist}
        artists = {t.tags.artist for t in tracks if t.tags.artist}
        if len(album_artists) == 1:
            artist = album_artists.pop()
        elif len(artists) == 1:
            artist = artists.pop()
        elif artists:
            artist = VARIOUS_ARTISTS
        else:
            artist = ""
        if not artist or not album:
            folder = source_dir[-1] if source_dir else self.archive_stem
            guessed_artist, guessed_album = _guess_from_folder(folder)
            artist = artist or guessed_artist or UNKNOWN_ARTIST
            album = album or guessed_album or self.archive_stem
        return artist, album

    def track_dir(self, source_dir):
        return self.dirs[source_dir]

    def image_dir(self, source_dir):
        """Images go next to the tracks of their folder, of a parent folder, or of their album."""
        if source_dir in self.dirs:
            return self.dirs[source_dir]
        for length in range(len(source_dir) - 1, -1, -1):
            ancestor = source_dir[:length]
            if ancestor in self.dirs:
                sub = tuple(safe_folder_name(part) for part in source_dir[length:])
                return (self.dirs[ancestor] + sub)[:MAX_FOLDER_DEPTH]
        below = {self.targets[d] for d in self.dirs if d[: len(source_dir)] == source_dir}
        if len(below) == 1:
            return library_dir(*below.pop())
        return None


def process_archive(batch, uploaded_file, name):
    archive_type = "RAR" if is_rar_name(name) else "ZIP"
    with tempfile.TemporaryDirectory(prefix="extract-", dir=incoming_directory()) as stage_dir:
        if archive_type == "RAR":
            extracted = extract_rar(uploaded_file, batch.policy, stage_dir, batch.password)
        else:
            extracted = extract_zip(uploaded_file, batch.policy, stage_dir, batch.password)
        prepared, invalid = [], []
        for item in extracted:
            if item.kind != "audio":
                continue
            track = Track(
                original_name=f"{name}/{item.archive_path}",
                filename=safe_basename(item.name),
                source=item.path,
                size=item.size,
            )
            try:
                prepare_track(batch, track)
            except UploadRejected as exc:
                invalid.append((track, str(exc)))
                continue
            prepared.append((item, track))

        tracks_by_dir = {}
        for item, track in prepared:
            tracks_by_dir.setdefault(item.parts[:-1], []).append(track)
        layout = ArchiveLayout(tracks_by_dir, name)

        new_tracks, duplicates = [], []
        for item, track in prepared:
            track.rel_dir = layout.track_dir(item.parts[:-1])
            reason = batch.find_duplicate(track)
            if reason:
                duplicates.append((track, reason))
                batch.remember(track)
                continue
            batch.remember(track)
            new_tracks.append(track)

        for track, reason in duplicates:
            batch.record_duplicate(track, reason)
        details = [{"name": t.original_name, "status": "duplicate", "message": r} for t, r in duplicates]
        details += [{"name": t.original_name, "status": "failed", "message": r} for t, r in invalid]

        if not new_tracks:
            for track, reason in invalid:
                batch.record_failure(track.original_name, reason, track.size)
            if duplicates and not invalid:
                message = ngettext(
                    "%(count)d track from the archive is already in the library.",
                    "All %(count)d tracks from the archive are already in the library.",
                    len(duplicates),
                ) % {"count": len(duplicates)}
                return {"name": name, "status": "duplicate", "message": message, "details": details}
            if duplicates:
                message = _("Nothing was added: %(dup)d duplicate(s), %(bad)d invalid file(s).") % {
                    "dup": len(duplicates),
                    "bad": len(invalid),
                }
                return {"name": name, "status": "failed", "message": message, "details": details}
            raise UploadRejected(_("The archive contains no valid audio files."))

        batch.check_quota(sum(track.size for track in new_tracks))

        # Keep cover art and booklet scans that sit next to the new tracks.
        track_dirs = {track.rel_dir for track in new_tracks}
        images = []
        for item in extracted:
            if item.kind != "image" or item.size > MAX_IMAGE_SIZE:
                continue
            target_dir = layout.image_dir(item.parts[:-1])
            if target_dir is None:
                continue
            # Next to new tracks, or in the album folder above their disc folders.
            near_tracks = any(track_dir[: len(target_dir)] == target_dir for track_dir in track_dirs)
            if near_tracks and len(target_dir) >= 2 and looks_like_image(item.path):
                images.append((target_dir, safe_basename(item.name), item.path))

        for track in new_tracks:
            batch.seen_hashes.pop(track.sha256, None)
            batch.seen_keys.pop(track.tags.key, None)
        batch.store_tracks(
            new_tracks,
            images,
            success_message=_("Extracted from %(type)s and added to the shared music library.") % {"type": archive_type},
        )
        for track, reason in invalid:
            batch.record_failure(track.original_name, reason, track.size)

    folders = sorted({"Uploads/" + "/".join(track.rel_dir) for track in new_tracks})
    message = ngettext(
        "Added %(count)d track to %(folder)s.",
        "Added %(count)d tracks to %(folder)s.",
        len(new_tracks),
    ) % {"count": len(new_tracks), "folder": ", ".join(folders)}
    if duplicates:
        message += " " + ngettext(
            "Skipped %(count)d duplicate.",
            "Skipped %(count)d duplicates.",
            len(duplicates),
        ) % {"count": len(duplicates)}
    if invalid:
        message += " " + ngettext(
            "Skipped %(count)d invalid file.",
            "Skipped %(count)d invalid files.",
            len(invalid),
        ) % {"count": len(invalid)}
    status = "success" if not duplicates and not invalid else "partial"
    return {"name": name, "status": status, "message": message, "details": details}


def process_upload(batch, uploaded_file):
    name = safe_basename(uploaded_file.name)
    try:
        if uploaded_file.size > batch.policy.max_file_size:
            raise UploadRejected(_("The file exceeds the configured per-file size limit."))
        if is_zip_name(name) or is_rar_name(name):
            return process_archive(batch, uploaded_file, name)
        return process_single_audio(batch, uploaded_file, name)
    except UploadRejected as exc:
        batch.record_failure(name, str(exc), uploaded_file.size)
        return {"name": name, "status": "failed", "message": str(exc)}
    except OSError:
        logger.exception("Upload storage failure for user id=%s", batch.user.pk)
        message = _("Server storage is unavailable.")
        batch.record_failure(name, message, uploaded_file.size)
        return {"name": name, "status": "failed", "message": message}
    except Exception:
        logger.exception("Unexpected upload failure for user id=%s", batch.user.pk)
        message = _("The server could not process this file.")
        batch.record_failure(name, message, uploaded_file.size)
        return {"name": name, "status": "failed", "message": message}
