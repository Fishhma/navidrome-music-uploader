import os
import re
import zipfile
from pathlib import PurePosixPath

import pyzipper
import rarfile

# Avoid rarfile's temporary-file extraction hack: large uploads use /incoming.
rarfile.USE_EXTRACT_HACK = 0

from django.conf import settings
from django.utils.translation import gettext as _

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".opus", ".m4a", ".m4b", ".aac", ".aiff", ".aif", ".wma"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | IMAGE_EXTENSIONS
MAX_NAME_LENGTH = 180


class UploadRejected(ValueError):
    pass


class ArchivePasswordRequired(UploadRejected):
    pass


class ArchivePasswordInvalid(UploadRejected):
    pass


def safe_basename(name):
    name = str(name).replace("\\", "/").split("/")[-1]
    name = "".join(char for char in name if char.isprintable() and char not in "/\\:")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "audio"
    return name[:MAX_NAME_LENGTH]


def safe_relative_path(name):
    normalized = str(name).replace("\\", "/")
    path = PurePosixPath(normalized)
    if (not normalized or path.is_absolute() or ".." in path.parts or
            any(":" in part or "\x00" in part for part in path.parts)):
        raise UploadRejected(_("The supplied folder path is unsafe."))
    parts = [safe_basename(part) for part in path.parts if part not in {"", "."}]
    if not parts:
        raise UploadRejected(_("The supplied folder path is unsafe."))
    return PurePosixPath(*parts)


def inspect_audio(stream, name):
    extension = PurePosixPath(name.lower()).suffix
    if extension not in AUDIO_EXTENSIONS:
        raise UploadRejected(_("Unsupported file type. ZIP, RAR, or a supported audio format is required."))
    stream.seek(0)
    header = stream.read(8192)
    stream.seek(0)
    valid = False
    if extension == ".mp3":
        valid = header.startswith(b"ID3") or any(header[i] == 0xFF and header[i + 1] & 0xE0 == 0xE0 for i in range(len(header) - 1))
    elif extension == ".flac":
        valid = header.startswith(b"fLaC")
    elif extension == ".wav":
        valid = len(header) >= 12 and header[:4] in {b"RIFF", b"RF64"} and header[8:12] == b"WAVE"
    elif extension in {".aiff", ".aif"}:
        valid = len(header) >= 12 and header[:4] == b"FORM" and header[8:12] in {b"AIFF", b"AIFC"}
    elif extension in {".ogg", ".opus"}:
        valid = header.startswith(b"OggS")
    elif extension in {".m4a", ".m4b"}:
        valid = len(header) >= 8 and header[4:8] == b"ftyp"
    elif extension == ".aac":
        valid = len(header) >= 2 and header[0] == 0xFF and header[1] & 0xF6 == 0xF0
    elif extension == ".wma":
        valid = header.startswith(bytes.fromhex("3026B2758E66CF11A6D900AA0062CE6C"))
    if not valid:
        raise UploadRejected(_("The file contents do not match a supported audio format."))
    return extension


def inspect_image(stream, name):
    extension = PurePosixPath(name.lower()).suffix
    if extension not in IMAGE_EXTENSIONS:
        raise UploadRejected(_("Unsupported image format."))
    stream.seek(0)
    header = stream.read(32)
    stream.seek(0)
    valid = (
        (extension in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff")) or
        (extension == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n")) or
        (extension == ".webp" and len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP") or
        (extension == ".gif" and header[:6] in {b"GIF87a", b"GIF89a"}) or
        (extension == ".bmp" and header.startswith(b"BM"))
    )
    if not valid:
        raise UploadRejected(_("The file contents do not match a supported image format."))
    return extension


def incoming_directory():
    directory = settings.UPLOAD_INCOMING_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _validate_member_name(info):
    normalized_name = info.filename.replace("\\", "/")
    path = PurePosixPath(normalized_name)
    mode = getattr(info, "external_attr", 0) >> 16
    if (path.is_absolute() or ".." in path.parts or not info.filename or
            ":" in normalized_name or "\x00" in normalized_name or
            (mode & 0o170000) == 0o120000):
        raise UploadRejected(_("The archive contains an unsafe path or link."))
    return path


def validate_archive(uploaded_file, policy, password=None):
    uploaded_file.seek(0)
    if not zipfile.is_zipfile(uploaded_file):
        raise UploadRejected(_("The ZIP archive is invalid."))
    uploaded_file.seek(0)
    archive = None
    try:
        archive = pyzipper.AESZipFile(uploaded_file, mode="r")
        infos = archive.infolist()
        file_infos = [info for info in infos if not info.is_dir()]
        if len(file_infos) > policy.max_archive_entries:
            raise UploadRejected(_("The ZIP archive contains too many files."))
        encrypted = any(info.flag_bits & 0x1 for info in file_infos)
        if encrypted and not password:
            raise ArchivePasswordRequired(_("This ZIP archive is password-protected."))
        if password:
            archive.setpassword(password.encode("utf-8"))
        total_size = 0
        media_infos = []
        for info in infos:
            path = _validate_member_name(info)
            if info.is_dir():
                continue
            total_size += info.file_size
            if total_size > policy.max_archive_expanded_size:
                raise UploadRejected(_("The ZIP archive expands beyond the configured size limit."))
            if info.file_size and (not info.compress_size or info.file_size / info.compress_size > policy.max_archive_ratio):
                raise UploadRejected(_("The ZIP archive exceeds the configured compression ratio."))
            if path.suffix.lower() in MEDIA_EXTENSIONS:
                media_infos.append(info)
        if not media_infos:
            raise UploadRejected(_("The ZIP archive contains no supported audio files or cover images."))
        return archive, media_infos, total_size
    except ArchivePasswordRequired:
        if archive:
            archive.close()
        raise
    except (RuntimeError, zipfile.BadZipFile) as exc:
        if archive:
            archive.close()
        if password:
            raise ArchivePasswordInvalid(_("The ZIP password is incorrect or the archive is damaged.")) from exc
        raise UploadRejected(_("The ZIP archive is invalid or cannot be read.")) from exc
    except UploadRejected:
        if archive:
            archive.close()
        raise
    except Exception as exc:
        if archive:
            archive.close()
        raise UploadRejected(_("The ZIP archive is invalid or cannot be read.")) from exc


def validate_rar(archive_path, policy, password=None):
    archive = None
    try:
        archive = rarfile.RarFile(archive_path)
        if password:
            archive.setpassword(password)
        if not password and archive.needs_password():
            raise ArchivePasswordRequired(_("This RAR archive is password-protected."))
        infos = archive.infolist()
        file_infos = [info for info in infos if not info.is_dir()]
        if len(file_infos) > policy.max_archive_entries:
            raise UploadRejected(_("The RAR archive contains too many files."))
        if not file_infos and archive.needs_password() and not password:
            raise ArchivePasswordRequired(_("This RAR archive is password-protected."))
        total_size = 0
        media_infos = []
        for info in infos:
            normalized_name = info.filename.replace("\\", "/")
            path = PurePosixPath(normalized_name)
            is_symlink = getattr(info, "is_symlink", None)
            if callable(is_symlink):
                is_symlink = is_symlink()
            mode = getattr(info, "mode", 0) or 0
            if (path.is_absolute() or ".." in path.parts or not info.filename or
                    ":" in normalized_name or "\x00" in normalized_name or
                    bool(is_symlink) or (mode & 0o170000) == 0o120000):
                raise UploadRejected(_("The RAR archive contains an unsafe path or link."))
            if info.is_dir():
                continue
            total_size += info.file_size
            if total_size > policy.max_archive_expanded_size:
                raise UploadRejected(_("The RAR archive expands beyond the configured size limit."))
            compressed_size = getattr(info, "compress_size", 0) or 0
            if info.file_size and (not compressed_size or info.file_size / compressed_size > policy.max_archive_ratio):
                raise UploadRejected(_("The RAR archive exceeds the configured compression ratio."))
            needs_password = getattr(info, "needs_password", False)
            if callable(needs_password):
                needs_password = needs_password()
            if needs_password and not password:
                raise ArchivePasswordRequired(_("This RAR archive is password-protected."))
            if path.suffix.lower() in MEDIA_EXTENSIONS:
                media_infos.append(info)
        if not media_infos:
            raise UploadRejected(_("The RAR archive contains no supported audio files or cover images."))
        return archive, media_infos, total_size
    except (ArchivePasswordRequired, UploadRejected):
        if archive:
            archive.close()
        raise
    except rarfile.RarWrongPassword as exc:
        if archive:
            archive.close()
        raise ArchivePasswordInvalid(_("The RAR password is incorrect.")) from exc
    except rarfile.PasswordRequired as exc:
        if archive:
            archive.close()
        if password:
            raise ArchivePasswordInvalid(_("The RAR password is incorrect.")) from exc
        raise ArchivePasswordRequired(_("This RAR archive is password-protected.")) from exc
    except (rarfile.Error, OSError, ValueError) as exc:
        if archive:
            archive.close()
        if password and isinstance(exc, rarfile.RarCRCError):
            raise ArchivePasswordInvalid(_("The RAR password is incorrect.")) from exc
        raise UploadRejected(_("The RAR archive is invalid or cannot be read.")) from exc


def is_archive_name(name):
    return os.path.splitext(name.lower())[1] in {".zip", ".rar"}


def is_zip_name(name):
    return os.path.splitext(name.lower())[1] == ".zip"
