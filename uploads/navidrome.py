"""Optional Navidrome (Subsonic API) integration.

Configured with NAVIDROME_URL, NAVIDROME_USERNAME and NAVIDROME_PASSWORD. When configured, the uploader
checks the whole Navidrome library for duplicates and starts a library scan after a successful upload.
Every call is best-effort: if Navidrome is unreachable the upload still works.
"""

import hashlib
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

from .tags import normalize

logger = logging.getLogger(__name__)

API_VERSION = "1.16.1"
CLIENT_NAME = "music-uploader"


class NavidromeClient:
    def __init__(self, url=None, username=None, password=None, timeout=None):
        self.url = (url if url is not None else getattr(settings, "NAVIDROME_URL", "")).rstrip("/")
        self.username = username if username is not None else getattr(settings, "NAVIDROME_USERNAME", "")
        self.password = password if password is not None else getattr(settings, "NAVIDROME_PASSWORD", "")
        self.timeout = timeout or getattr(settings, "NAVIDROME_TIMEOUT", 5)
        self._available = None
        self._search_cache = {}

    @property
    def enabled(self):
        return bool(self.url and self.username and self.password) and self._available is not False

    def _call(self, endpoint, **params):
        salt = secrets.token_hex(8)
        query = {
            "u": self.username,
            "t": hashlib.md5((self.password + salt).encode("utf-8")).hexdigest(),
            "s": salt,
            "v": API_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
            **params,
        }
        request_url = f"{self.url}/rest/{endpoint}?{urllib.parse.urlencode(query)}"
        with urllib.request.urlopen(request_url, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        body = payload.get("subsonic-response", {})
        if body.get("status") != "ok":
            error = body.get("error", {})
            raise RuntimeError(f"Navidrome error {error.get('code')}: {error.get('message')}")
        return body

    def find_song(self, artist, album, title):
        """Return the first library song with the same artist, title and (if known) album."""
        if not self.enabled or not artist or not title:
            return None
        wanted_artist, wanted_album, wanted_title = normalize(artist), normalize(album), normalize(title)
        query = f"{title} {artist}"
        songs = self._search_cache.get(query)
        if songs is None:
            try:
                body = self._call("search3", query=query, songCount=50, artistCount=0, albumCount=0)
            except (OSError, ValueError, RuntimeError) as exc:
                # Do not keep retrying (and waiting for timeouts) for the rest of this request.
                logger.warning("Navidrome duplicate check unavailable: %s", exc)
                self._available = False
                return None
            songs = body.get("searchResult3", {}).get("song", []) or []
            self._search_cache[query] = songs
        for song in songs:
            if normalize(song.get("title")) != wanted_title:
                continue
            artists = {normalize(song.get("artist")), normalize(song.get("albumArtist")), normalize(song.get("displayArtist"))}
            if wanted_artist not in artists:
                continue
            if wanted_album and normalize(song.get("album")) != wanted_album:
                continue
            return song
        return None

    def start_scan(self):
        if not (self.url and self.username and self.password):
            return False
        try:
            self._call("startScan")
            return True
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning("Could not start a Navidrome library scan: %s", exc)
            return False
