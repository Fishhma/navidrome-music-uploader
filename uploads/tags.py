"""Audio tag reading and normalization for tag-based duplicate detection."""

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    import mutagen
except ImportError:  # pragma: no cover - mutagen is in requirements.txt
    mutagen = None


@dataclass
class TrackTags:
    artist: str = ""
    album: str = ""
    title: str = ""
    albumartist: str = ""

    @property
    def album_artist(self):
        return self.albumartist or self.artist

    @property
    def key(self):
        return track_key(self.artist, self.album, self.title)

    def describe(self):
        text = " — ".join(part for part in (self.artist, self.title) if part)
        if self.album:
            text = f"{text} ({self.album})" if text else self.album
        return text


def _first(tags, *names):
    for name in names:
        value = tags.get(name)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value:
            return str(value).strip()[:300]
    return ""


def read_tags(path):
    if mutagen is None:
        return TrackTags()
    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception as exc:  # mutagen raises many different errors for odd files
        logger.info("Could not read tags from %s: %s", path, exc)
        return TrackTags()
    if audio is None or not audio.tags:
        return TrackTags()
    tags = audio.tags
    return TrackTags(
        artist=_first(tags, "artist", "albumartist"),
        album=_first(tags, "album"),
        title=_first(tags, "title"),
        albumartist=_first(tags, "albumartist"),
    )


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^\w]+", " ", value)
    return " ".join(value.split())


def track_key(artist, album, title):
    artist, album, title = normalize(artist), normalize(album), normalize(title)
    if not artist or not title:
        return ""
    return hashlib.sha256(f"{artist}\x1f{album}\x1f{title}".encode("utf-8")).hexdigest()
