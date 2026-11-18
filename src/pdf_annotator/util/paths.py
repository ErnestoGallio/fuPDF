"""Lightweight source identity. Call filesystem operations outside the GUI thread."""

from dataclasses import dataclass
import os
from pathlib import Path
import stat


@dataclass(frozen=True)
class FileIdentity:
    canonical_path: str
    size: int
    mtime_ns: int
    device: int
    inode: int

    @classmethod
    def from_stat(cls, canonical_path: str, info: os.stat_result) -> "FileIdentity":
        return cls(canonical_path, info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino)

    @property
    def path_key(self) -> str:
        return os.path.normcase(self.canonical_path)

    @property
    def file_key(self) -> tuple[int, int] | None:
        # os.stat (not DirEntry.stat) provides the Windows file index when available.
        return (self.device, self.inode) if self.inode else None


def identify_file(path: str) -> FileIdentity:
    resolved = Path(path).resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Not a regular file")
    if resolved.suffix.lower() != ".pdf":
        raise ValueError("Not a PDF path")
    return FileIdentity.from_stat(str(resolved), info)


@dataclass(frozen=True)
class SaveTarget:
    canonical_path: str
    existing: FileIdentity | None


def identify_save_target(path: str) -> SaveTarget:
    resolved = Path(path).resolve()
    if resolved.suffix.lower() != ".pdf" or not resolved.parent.is_dir():
        raise ValueError("Invalid PDF destination")
    return SaveTarget(str(resolved), identify_file(str(resolved)) if resolved.exists() else None)
