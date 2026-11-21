"""Safe extraction of ZIP and RAR uploads into a private staging directory.

Only regular audio and image members are extracted and returned. Everything else in the archive
(text files, playlists, logs, links) is ignored.
"""

import logging
import os
import shutil
import subprocess
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import rarfile
from django.conf import settings
from django.utils.translation import gettext as _

from .validators import UploadRejected, is_audio_name, is_image_name, is_unsafe_member_path

logger = logging.getLogger(__name__)

COPY_CHUNK = 1024 * 1024
UNRAR_TIMEOUT_SECONDS = 900


@dataclass
class ExtractedFile:
    # Path components inside the archive, e.g. ("Artist - Album", "01. Track.mp3").
    parts: tuple
    path: Path
    size: int
    kind: str  # "audio" or "image"

    @property
    def name(self):
        return self.parts[-1]

    @property
    def archive_path(self):
        return "/".join(self.parts)


@dataclass
class _Member:
    info: object
    parts: tuple
    size: int
    compressed: int
    kind: str


def _member_kind(name):
    if is_audio_name(name):
        return "audio"
    if is_image_name(name):
        return "image"
    return None


def _check_members(members, file_count, total_size, policy, label):
    if file_count > policy.max_archive_entries:
        raise UploadRejected(_("The %(type)s archive contains too many files.") % {"type": label})
    if total_size > policy.max_archive_expanded_size:
        raise UploadRejected(_("The %(type)s archive expands beyond the configured size limit.") % {"type": label})
    for member in members:
        if member.size and (not member.compressed or member.size / member.compressed > policy.max_archive_ratio):
            raise UploadRejected(_("The %(type)s archive exceeds the configured compression ratio.") % {"type": label})
    if not any(member.kind == "audio" for member in members):
        raise UploadRejected(_("The %(type)s archive contains no supported audio files.") % {"type": label})


def _split_parts(name):
    return tuple(part for part in PurePosixPath(name.replace("\\", "/")).parts if part not in ("", "."))


def uploaded_file_path(uploaded_file, stage_dir, suffix):
    """Return a real filesystem path for the upload, copying small in-memory uploads to the stage."""
    temporary_path = getattr(uploaded_file, "temporary_file_path", None)
    if temporary_path:
        try:
            return Path(temporary_path())
        except (AttributeError, NotImplementedError):
            pass
    path = Path(stage_dir) / f"upload{suffix}"
    uploaded_file.seek(0)
    with open(path, "wb") as target:
        shutil.copyfileobj(uploaded_file, target, COPY_CHUNK)
    return path


def _copy_limited(source, target_path, limit, label):
    written = 0
    with open(target_path, "wb") as target:
        while True:
            chunk = source.read(COPY_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise UploadRejected(_("The %(type)s archive expands beyond the configured size limit.") % {"type": label})
            target.write(chunk)
    return written


# --------------------------------------------------------------------------------------------- ZIP


def _zip_member_name(info):
    """Recover non-UTF-8 names written by Windows archivers (cp866 for Russian, cp852 for Czech)."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    if raw.isascii():
        return info.filename
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    candidates = []
    for encoding in ("cp866", "cp852", "cp1251", "cp1250"):
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        letters = sum(1 for char in decoded if not char.isascii() and char.isalpha())
        cyrillic = sum(1 for char in decoded if "Ѐ" <= char <= "ӿ")
        candidates.append((letters + cyrillic, decoded))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return info.filename


def extract_zip(uploaded_file, policy, stage_dir, password=None):
    uploaded_file.seek(0)
    if not zipfile.is_zipfile(uploaded_file):
        raise UploadRejected(_("The ZIP archive is invalid."))
    uploaded_file.seek(0)
    try:
        archive = zipfile.ZipFile(uploaded_file)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UploadRejected(_("The ZIP archive is invalid.")) from exc
    with archive:
        members = []
        file_count = 0
        total_size = 0
        encrypted = False
        for info in archive.infolist():
            name = _zip_member_name(info)
            if is_unsafe_member_path(name):
                raise UploadRejected(_("The %(type)s archive contains an unsafe path.") % {"type": "ZIP"})
            if info.is_dir():
                continue
            file_count += 1
            total_size += info.file_size
            mode = info.external_attr >> 16
            if (mode & 0o170000) == 0o120000:
                continue  # Symbolic link: never extracted.
            kind = _member_kind(name)
            parts = _split_parts(name)
            if not kind or not parts:
                continue
            if info.flag_bits & 0x1:
                encrypted = True
                if info.compress_type == 99:
                    raise UploadRejected(_("AES-encrypted ZIP archives are not supported. Use RAR or an unencrypted ZIP."))
            members.append(_Member(info, parts, info.file_size, info.compress_size, kind))
        _check_members(members, file_count, total_size, policy, "ZIP")
        if encrypted and not password:
            raise UploadRejected(_("This archive is password-protected. Enter the archive password."))

        extracted = []
        remaining = policy.max_archive_expanded_size
        pwd = password.encode("utf-8") if password else None
        for index, member in enumerate(members):
            target = Path(stage_dir) / f"{index:05d}"
            try:
                with archive.open(member.info, pwd=pwd) as source:
                    size = _copy_limited(source, target, remaining, "ZIP")
            except RuntimeError as exc:
                if "password" in str(exc).lower():
                    raise UploadRejected(_("The archive password is incorrect.")) from exc
                raise
            except NotImplementedError as exc:
                logger.warning("ZIP member %r uses an unsupported method: %s", member.info.filename, exc)
                raise UploadRejected(_("The ZIP archive uses an unsupported compression method.")) from exc
            except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
                logger.warning("ZIP member %r cannot be read: %s", member.info.filename, exc)
                if member.info.flag_bits & 0x1:
                    raise UploadRejected(_("The archive password is incorrect or the archive is damaged.")) from exc
                raise UploadRejected(_("The ZIP archive is damaged (%(name)s).") % {"name": member.parts[-1]}) from exc
            remaining -= size
            extracted.append(ExtractedFile(member.parts, target, size, member.kind))
        return extracted


# --------------------------------------------------------------------------------------------- RAR


def _rar_open_error(exc):
    if isinstance(exc, rarfile.PasswordRequired):
        return UploadRejected(_("This archive is password-protected. Enter the archive password."))
    if isinstance(exc, rarfile.RarWrongPassword):
        return UploadRejected(_("The archive password is incorrect."))
    if isinstance(exc, rarfile.NoCrypto):
        return UploadRejected(_("The server cannot read this encrypted RAR archive."))
    if isinstance(exc, rarfile.NeedFirstVolume):
        return UploadRejected(_("Multi-volume RAR archives are not supported. Upload a single-volume archive."))
    return UploadRejected(_("The RAR archive is invalid or the password is incorrect."))


def _unrar_tool():
    configured = getattr(settings, "UNRAR_TOOL", "") or "unrar"
    return shutil.which(configured)


def _list_rar(path, policy, password):
    archive = None
    try:
        archive = rarfile.RarFile(str(path))
        if password:
            # Re-reads the headers when they are encrypted.
            archive.setpassword(password)
        infos = archive.infolist()
        needs_password = archive.needs_password()
    except rarfile.Error as exc:
        if archive is not None:
            archive.close()
        raise _rar_open_error(exc) from exc
    except Exception:
        if archive is not None:
            archive.close()
        raise

    members = []
    file_count = 0
    total_size = 0
    try:
        if needs_password and not password:
            raise UploadRejected(_("This archive is password-protected. Enter the archive password."))
        for info in infos:
            name = info.filename
            if is_unsafe_member_path(name):
                raise UploadRejected(_("The %(type)s archive contains an unsafe path.") % {"type": "RAR"})
            if info.is_dir():
                continue
            file_count += 1
            total_size += info.file_size or 0
            if info.is_symlink() or getattr(info, "file_redir", None):
                continue
            kind = _member_kind(name)
            parts = _split_parts(name)
            if not kind or not parts:
                continue
            members.append(_Member(info, parts, info.file_size or 0, info.compress_size or 0, kind))
        _check_members(members, file_count, total_size, policy, "RAR")
    except Exception:
        archive.close()
        raise
    return archive, members, needs_password


def _extract_rar_with_unrar(tool, path, password, output_dir, encrypted):
    """Extract the whole archive in one pass (fast for solid archives) with the official unrar."""
    output_dir.mkdir()
    command = [tool, "x", "-y", "-o+", "-idq", "-cfg-", "-ai"]
    command.append(f"-p{password}" if password else "-p-")
    command += ["--", str(path), str(output_dir) + os.sep]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=UNRAR_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UploadRejected(_("Extracting the RAR archive took too long.")) from exc
    code = completed.returncode
    if code in (0, 1):  # 1 = non-fatal warning
        return
    details = (completed.stderr or completed.stdout or b"").decode("utf-8", "replace").strip()
    if password:
        details = details.replace(password, "***")
    logger.warning("unrar exited with code %s: %s", code, details[:2000])
    if code == 11:
        raise UploadRejected(_("The archive password is incorrect."))
    if code == 3:
        if encrypted:
            raise UploadRejected(_("The archive password is incorrect or the archive is damaged."))
        raise UploadRejected(_("The RAR archive is damaged (checksum error)."))
    if code == 8:
        raise UploadRejected(_("The server ran out of memory while extracting the archive."))
    if code == 5:
        raise OSError("unrar could not write extracted files")
    if "volume" in details.lower():
        raise UploadRejected(_("Multi-volume RAR archives are not supported. Upload a single-volume archive."))
    raise UploadRejected(_("The RAR archive could not be extracted."))


def _collect_unrar_output(output_dir):
    """Return the regular audio/image files unrar actually wrote, never following links."""
    root = output_dir.resolve()
    extracted = []
    for current, dirnames, filenames in os.walk(output_dir, followlinks=False):
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(current, name))]
        for filename in filenames:
            candidate = Path(current) / filename
            kind = _member_kind(filename)
            if not kind or os.path.islink(candidate) or not candidate.is_file():
                continue
            if not str(candidate.resolve()).startswith(str(root) + os.sep):
                continue
            parts = candidate.relative_to(output_dir).parts
            extracted.append(ExtractedFile(parts, candidate, candidate.stat().st_size, kind))
    extracted.sort(key=lambda item: [part.casefold() for part in item.parts])
    return extracted


def _extract_rar_with_rarfile(archive, members, password, stage_dir, policy):
    """Fallback when the official unrar is not installed (e.g. local development)."""
    extracted = []
    remaining = policy.max_archive_expanded_size
    for index, member in enumerate(members):
        target = Path(stage_dir) / f"{index:05d}"
        try:
            with archive.open(member.info, pwd=password) as source:
                size = _copy_limited(source, target, remaining, "RAR")
        except rarfile.RarCannotExec as exc:
            raise UploadRejected(_("RAR extraction is unavailable on the server.")) from exc
        except (rarfile.RarWrongPassword, rarfile.PasswordRequired) as exc:
            raise _rar_open_error(exc) from exc
        except (rarfile.BadRarFile, rarfile.RarCRCError) as exc:
            logger.warning("RAR member %r failed: %s", member.info.filename, exc)
            if member.info.needs_password():
                raise UploadRejected(_("The archive password is incorrect or the archive is damaged.")) from exc
            raise UploadRejected(_("The RAR archive is damaged (checksum error).")) from exc
        except rarfile.Error as exc:
            logger.warning("RAR member %r failed: %s", member.info.filename, exc)
            raise UploadRejected(_("The RAR archive could not extract %(name)s.") % {"name": member.parts[-1]}) from exc
        remaining -= size
        extracted.append(ExtractedFile(member.parts, target, size, member.kind))
    return extracted


def extract_rar(uploaded_file, policy, stage_dir, password=None):
    stage_dir = Path(stage_dir)
    path = uploaded_file_path(uploaded_file, stage_dir, ".rar")
    archive, members, encrypted = _list_rar(path, policy, password)
    try:
        tool = _unrar_tool()
        if tool:
            output_dir = stage_dir / "unrar"
            _extract_rar_with_unrar(tool, path, password, output_dir, encrypted)
            extracted = _collect_unrar_output(output_dir)
            if not any(item.kind == "audio" for item in extracted):
                raise UploadRejected(_("The RAR archive could not be extracted."))
            return extracted
        return _extract_rar_with_rarfile(archive, members, password, stage_dir, policy)
    finally:
        archive.close()
