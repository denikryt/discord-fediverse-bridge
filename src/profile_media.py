"""Secure local storage and advisory locking for immutable profile media."""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from PIL import Image, ImageOps, UnidentifiedImageError

MediaKind = Literal["avatar", "header"]


class ProfileMediaError(ValueError):
    """Report validation or storage failures without exposing source details."""


@dataclass(frozen=True, slots=True)
class PublishedMediaObject:
    """Describe canonical immutable bytes already published to durable storage."""

    public_id: str
    storage_key: str
    media_kind: MediaKind
    content_type: str
    byte_size: int
    width: int
    height: int
    sha256_hex: str


class FileLock:
    """Own one POSIX advisory lock descriptor for a configured deployment path."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    def acquire(self, *, exclusive: bool, timeout_seconds: float = 30.0) -> "FileLock":
        """Acquire a shared or exclusive lock before the timeout expires."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Lock files are part of the generation-safety boundary. Refuse a
        # redirected path and verify that the current process owns the file.
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise RuntimeError("lock path must not be a symlink")
        handle = self.path.open("a+b")
        stat = os.fstat(handle.fileno())
        if stat.st_uid != os.geteuid():
            handle.close()
            raise RuntimeError("lock file must be owned by the bridge user")
        os.chmod(self.path, 0o600)
        operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), operation)
                self._handle = handle
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError(f"timed out acquiring deployment lock: {self.path.name}")
                time.sleep(0.05)

    def close(self) -> None:
        """Release the lock and close its descriptor exactly once."""
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "FileLock":
        """Return an already-acquired lock for context-manager use."""
        if self._handle is None:
            raise RuntimeError("lock must be acquired before entering context")
        return self

    def __exit__(self, *_args: object) -> None:
        """Release the advisory lock on context exit."""
        self.close()


class ProfileMediaStore:
    """Validate, canonicalize, publish, resolve, and unlink local profile images."""

    def __init__(self, root: Path, *, max_upload_bytes: int, max_pixels: int) -> None:
        self.root = root.absolute()
        self.staging_root = self.root / ".staging"
        self.objects_root = self.root / "objects"
        self.max_upload_bytes = max_upload_bytes
        self.max_pixels = max_pixels

    def initialize(self) -> None:
        """Create private storage directories and reject redirected volume paths."""
        # Every existing path component is checked before mkdir so a malicious
        # pre-created volume cannot redirect durable writes through a symlink.
        for path in (self.root, self.staging_root, self.objects_root):
            if path.exists() and path.is_symlink():
                raise RuntimeError("profile media storage directories must not be symlinks")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging_root.mkdir(exist_ok=True, mode=0o700)
        self.objects_root.mkdir(exist_ok=True, mode=0o700)
        for path in (self.root, self.staging_root, self.objects_root):
            stat = path.stat(follow_symlinks=False)
            if stat.st_uid != os.geteuid():
                raise RuntimeError("profile media storage must be owned by the bridge user")
            if not os.access(path, os.W_OK | os.X_OK):
                raise RuntimeError("profile media storage is not writable")

    def stage_and_publish(self, kind: MediaKind, source_stream: BinaryIO) -> PublishedMediaObject:
        """Publish canonical still-image bytes before any database reference commits."""
        if kind not in ("avatar", "header"):
            raise ProfileMediaError("unsupported profile media kind")
        token = secrets.token_urlsafe(24)
        source_path = self.staging_root / f"{token}.upload"
        canonical_path = self.staging_root / f"{token}.canonical"
        try:
            total = 0
            with source_path.open("xb") as destination:
                while chunk := source_stream.read(64 * 1024):
                    total += len(chunk)
                    if total > self.max_upload_bytes:
                        raise ProfileMediaError("profile image exceeds compressed byte limit")
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if total == 0:
                raise ProfileMediaError("profile image is empty")

            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(source_path) as image:
                        if image.format not in {"JPEG", "PNG", "WEBP"}:
                            raise ProfileMediaError("unsupported profile image format")
                        if getattr(image, "n_frames", 1) != 1:
                            raise ProfileMediaError("animated profile images are not supported")
                        width, height = image.size
                        if width <= 0 or height <= 0 or width * height > self.max_pixels:
                            raise ProfileMediaError("profile image exceeds decoded pixel limit")
                        image.seek(0)
                        image.load()
                        normalized = ImageOps.exif_transpose(image)
                        has_alpha = normalized.mode in {"RGBA", "LA"} or (
                            normalized.mode == "P" and "transparency" in normalized.info
                        )
                        output = normalized.convert("RGBA" if has_alpha else "RGB")
                        width, height = output.size
                        content_type = "image/png" if has_alpha else "image/jpeg"
                        output_format = "PNG" if has_alpha else "JPEG"
                        save_options = {"optimize": True}
                        if output_format == "JPEG":
                            save_options.update({"quality": 90, "progressive": True})
                        with canonical_path.open("xb") as canonical:
                            output.save(canonical, format=output_format, **save_options)
                            canonical.flush()
                            os.fsync(canonical.fileno())
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                raise ProfileMediaError("invalid profile image") from exc

            canonical_bytes = canonical_path.read_bytes()
            sha256_hex = hashlib.sha256(canonical_bytes).hexdigest()
            public_id = secrets.token_urlsafe(24)
            extension = "png" if content_type == "image/png" else "jpg"
            storage_key = f"{public_id[:2]}/{public_id}.{extension}"
            target = self.resolve_storage_key(storage_key, require_exists=False)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(canonical_path, target)
            self._fsync_directory(target.parent)
            self._fsync_directory(self.objects_root)
            return PublishedMediaObject(
                public_id=public_id,
                storage_key=storage_key,
                media_kind=kind,
                content_type=content_type,
                byte_size=len(canonical_bytes),
                width=width,
                height=height,
                sha256_hex=sha256_hex,
            )
        finally:
            source_path.unlink(missing_ok=True)
            canonical_path.unlink(missing_ok=True)

    def resolve_storage_key(self, storage_key: str, *, require_exists: bool = True) -> Path:
        """Resolve a generated relative key while preventing traversal and symlink escape."""
        candidate = self.objects_root / storage_key
        resolved_parent = candidate.parent.resolve()
        if self.objects_root.resolve() not in (resolved_parent, *resolved_parent.parents):
            raise ProfileMediaError("invalid profile media storage key")
        if any(part in {"", ".", ".."} for part in Path(storage_key).parts):
            raise ProfileMediaError("invalid profile media storage key")
        if require_exists and (not candidate.is_file() or candidate.is_symlink()):
            raise FileNotFoundError(storage_key)
        return candidate

    def unlink(self, storage_key: str) -> None:
        """Delete one immutable object only after the caller establishes exclusion."""
        self.resolve_storage_key(storage_key).unlink()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Persist directory-entry changes across host crashes."""
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
