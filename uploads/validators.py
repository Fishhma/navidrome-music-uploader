import os
import re
import zipfile
from pathlib import PurePosixPath

from django.conf import settings


AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".opus", ".m4a", ".m4b", ".aac", ".aiff", ".aif", ".wma"}
MAX_NAME_LENGTH = 180


class UploadRejected(ValueError):
    pass


def safe_basename(name):
    # Discard both POSIX and Windows path components, even on Linux.
    name = str(name).replace("\\", "/").split("/")[-1]
    name = "".join(char for char in name if char.isprintable() and char not in "/\\:")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "audio"
    return name[:MAX_NAME_LENGTH]


def inspect_audio(stream, name):
    extension = PurePosixPath(name.lower()).suffix
    if extension not in AUDIO_EXTENSIONS:
        raise UploadRejected("Unsupported file type. ZIP or a supported audio format is required.")
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
        raise UploadRejected("The file contents do not match a supported audio format.")
    return extension


def validate_archive(uploaded_file, policy):
    uploaded_file.seek(0)
    if not zipfile.is_zipfile(uploaded_file):
        raise UploadRejected("The ZIP archive is invalid.")
    uploaded_file.seek(0)
    archive = None
    try:
        archive = zipfile.ZipFile(uploaded_file)
        infos = archive.infolist()
        if len(infos) > policy.max_archive_entries:
            raise UploadRejected("The ZIP archive contains too many entries.")
        total_size = 0
        audio_infos = []
        for info in infos:
            normalized_name = info.filename.replace("\\", "/")
            path = PurePosixPath(normalized_name)
            mode = info.external_attr >> 16
            if path.is_absolute() or ".." in path.parts or not info.filename or ":" in normalized_name or "\x00" in normalized_name or (mode & 0o170000) == 0o120000:
                raise UploadRejected("The ZIP archive contains an unsafe path or link.")
            if info.is_dir():
                continue
            total_size += info.file_size
            if total_size > policy.max_archive_expanded_size:
                raise UploadRejected("The ZIP archive expands beyond the configured size limit.")
            if info.file_size and (not info.compress_size or info.file_size / info.compress_size > policy.max_archive_ratio):
                raise UploadRejected("The ZIP archive exceeds the configured compression ratio.")
            if info.flag_bits & 0x1:
                raise UploadRejected("Encrypted ZIP archives are not supported.")
            extension = PurePosixPath(normalized_name.lower()).suffix
            if extension in AUDIO_EXTENSIONS:
                audio_infos.append(info)
        if not audio_infos:
            raise UploadRejected("The ZIP archive contains no supported audio files.")
        return archive, audio_infos, total_size
    except UploadRejected:
        if archive:
            archive.close()
        raise
    except zipfile.BadZipFile as exc:
        if archive:
            archive.close()
        raise UploadRejected("The ZIP archive is invalid.") from exc


def incoming_directory():
    directory = settings.UPLOAD_INCOMING_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def is_zip_name(name):
    return os.path.splitext(name.lower())[1] == ".zip"
