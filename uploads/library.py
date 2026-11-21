"""Writing files into <library>/Uploads without ever overwriting an existing file."""

import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from django.conf import settings

COPY_CHUNK = 1024 * 1024


def uploads_root():
    return Path(settings.UPLOAD_LIBRARY_DIR) / "Uploads"


def _candidate_names(filename):
    stem, suffix = os.path.splitext(filename)
    yield filename
    for number in range(2, 1000):
        yield f"{stem} ({number}){suffix}"


class LibraryWriter:
    """Stores files and remembers what it created so a failed upload can be rolled back."""

    def __init__(self):
        self.created_files = []
        self.created_dirs = []

    def _ensure_dir(self, rel_dir):
        root = uploads_root()
        root.mkdir(parents=True, exist_ok=True)
        current = root
        for part in rel_dir:
            current = current / part
            if not current.exists():
                current.mkdir()
                self.created_dirs.append(current)
            elif current.is_symlink() or not current.is_dir():
                raise OSError(f"{current} is not a directory")
        return current

    def store(self, source, rel_dir, filename, skip_if_exists=False):
        """Copy ``source`` (a path or a binary stream) to Uploads/<rel_dir>/<filename>.

        Returns the stored path relative to Uploads/ (POSIX style), or None when ``skip_if_exists``
        is set and a file with that name is already there.
        """
        target_dir = self._ensure_dir(rel_dir)
        if skip_if_exists and (target_dir / filename).exists():
            return None
        # Stage beside the destination so the final rename is atomic for Navidrome's scanner.
        fd, staged_path = tempfile.mkstemp(prefix=".upload-", suffix=".part", dir=target_dir)
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "wb") as staged:
                if isinstance(source, (str, os.PathLike)):
                    with open(source, "rb") as handle:
                        shutil.copyfileobj(handle, staged, COPY_CHUNK)
                else:
                    source.seek(0)
                    shutil.copyfileobj(source, staged, COPY_CHUNK)
                staged.flush()
                os.fsync(staged.fileno())
            final_path = self._publish(staged_path, target_dir, filename, skip_if_exists)
        finally:
            try:
                os.unlink(staged_path)
            except FileNotFoundError:
                pass
        if final_path is None:
            return None
        self.created_files.append(final_path)
        return PurePosixPath(*final_path.relative_to(uploads_root()).parts).as_posix()

    @staticmethod
    def _publish(staged_path, target_dir, filename, skip_if_exists):
        names = [filename] if skip_if_exists else _candidate_names(filename)
        for name in names:
            final_path = target_dir / name
            try:
                # link() never replaces an existing file, unlike rename().
                os.link(staged_path, final_path)
                return final_path
            except FileExistsError:
                continue
            except OSError:
                # Filesystems without hard links: fall back to a checked rename.
                if final_path.exists():
                    continue
                os.replace(staged_path, final_path)
                return final_path
        if skip_if_exists:
            return None
        raise OSError(f"Too many files named {filename!r} in {target_dir}")

    def rollback(self):
        for path in reversed(self.created_files):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for directory in reversed(self.created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        self.created_files.clear()
        self.created_dirs.clear()
