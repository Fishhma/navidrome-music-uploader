import os
import re
import unicodedata
from pathlib import PurePosixPath

from django.conf import settings
from django.utils.translation import gettext as _


AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".opus", ".m4a", ".m4b", ".aac", ".aiff", ".aif", ".wma"}
# Images are kept only when they come from an archive next to audio files, so Navidrome can show album art.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_IMAGE_SIZE = 50 * 1024 * 1024
MAX_NAME_LENGTH = 180
MAX_FOLDER_DEPTH = 4


class UploadRejected(ValueError):
    pass


def safe_basename(name):
    # Discard both POSIX and Windows path components, even on Linux.
    name = str(name).replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(char for char in name if char.isprintable() and char not in '/\\:*?"<>|')
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "audio"
    if len(name) > MAX_NAME_LENGTH:
        stem, suffix = os.path.splitext(name)
        suffix = suffix if len(suffix) <= 10 else ""
        name = stem[: MAX_NAME_LENGTH - len(suffix)].rstrip(" .") + suffix
    return name


def safe_folder_name(name):
    folder = safe_basename(name)
    # A folder named like a hidden/staging file would be confusing in the library.
    return folder.lstrip(".") or "Album"


def extension_of(name):
    return PurePosixPath(str(name).lower()).suffix


def is_audio_name(name):
    return extension_of(name) in AUDIO_EXTENSIONS


def is_image_name(name):
    return extension_of(name) in IMAGE_EXTENSIONS


def inspect_audio(stream, name):
    extension = extension_of(name)
    if extension not in AUDIO_EXTENSIONS:
        raise UploadRejected(_("Unsupported file type. Upload audio files or ZIP/RAR archives."))
    stream.seek(0)
    header = stream.read(8192)
    stream.seek(0)
    valid = False
    if extension == ".mp3":
        valid = header.startswith(b"ID3") or any(header[i] == 0xFF and header[i + 1] & 0xE0 == 0xE0 for i in range(len(header) - 1))
    elif extension == ".flac":
        # Some taggers prepend an ID3v2 block to FLAC files.
        valid = header.startswith(b"fLaC") or (header.startswith(b"ID3") and b"fLaC" in header)
    elif extension == ".wav":
        valid = len(header) >= 12 and header[:4] in {b"RIFF", b"RF64"} and header[8:12] == b"WAVE"
    elif extension in {".aiff", ".aif"}:
        valid = len(header) >= 12 and header[:4] == b"FORM" and header[8:12] in {b"AIFF", b"AIFC"}
    elif extension in {".ogg", ".opus"}:
        valid = header.startswith(b"OggS")
    elif extension in {".m4a", ".m4b"}:
        valid = len(header) >= 8 and header[4:8] == b"ftyp"
    elif extension == ".aac":
        valid = header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xF6 == 0xF0)
    elif extension == ".wma":
        valid = header.startswith(bytes.fromhex("3026B2758E66CF11A6D900AA0062CE6C"))
    if not valid:
        raise UploadRejected(_("The file contents do not match a supported audio format."))
    return extension


def looks_like_image(path):
    with open(path, "rb") as handle:
        header = handle.read(16)
    return (
        header.startswith(b"\xff\xd8\xff")
        or header.startswith(b"\x89PNG\r\n\x1a\n")
        or header[:6] in {b"GIF87a", b"GIF89a"}
        or (header[:4] == b"RIFF" and header[8:12] == b"WEBP")
    )


def is_unsafe_member_path(name):
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or re.match(r"^[A-Za-z]:", normalized) is not None
        or "\x00" in normalized
    )


def incoming_directory():
    directory = settings.UPLOAD_INCOMING_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def is_zip_name(name):
    return os.path.splitext(name.lower())[1] == ".zip"


def is_rar_name(name):
    return os.path.splitext(name.lower())[1] == ".rar"
