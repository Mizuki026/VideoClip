from __future__ import annotations

import errno
import importlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID


class StorageFailure(OSError):
    """A bounded, path-free storage error suitable for API diagnostics."""


@dataclass
class WriterLease:
    handle: BinaryIO
    path: Path
    locked: bool = True

    def release(self) -> None:
        if not self.locked:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl: Any = importlib.import_module("fcntl")

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.locked = False
            self.handle.close()


class MediaStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.incoming = self.root / "incoming"
        self.media = self.root / "media"
        self.leases = self.root / "leases"
        self.analysis = self.root / "analysis"

    def prepare(self) -> None:
        for directory in (self.incoming, self.media, self.leases, self.analysis):
            directory.mkdir(parents=True, exist_ok=True)

    def part_path(self, upload_id: UUID) -> Path:
        return self._path(self.incoming, f"{upload_id.hex}.part")

    def target_path(self, storage_name: str) -> Path:
        if not _valid_storage_name(storage_name):
            raise StorageFailure(errno.EINVAL, "invalid generated storage name")
        return self._path(self.media, f"{storage_name}.media")

    def lease_path(self, upload_id: UUID) -> Path:
        return self._path(self.leases, f"{upload_id.hex}.lock")

    @staticmethod
    def _path(parent: Path, name: str) -> Path:
        path = parent / name
        if path.parent != parent or path.is_symlink():
            raise StorageFailure(errno.EINVAL, "invalid controlled storage path")
        return path

    def acquire_lease(self, upload_id: UUID) -> WriterLease | None:
        path = self.lease_path(upload_id)
        handle = path.open("a+b", buffering=0)
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    handle.close()
                    return None
            else:
                fcntl: Any = importlib.import_module("fcntl")

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    return None
        except BaseException:
            handle.close()
            raise
        return WriterLease(handle, path)

    @staticmethod
    def write_chunk(handle: BinaryIO, chunk: bytes) -> None:
        handle.write(chunk)

    @staticmethod
    def flush_part(handle: BinaryIO) -> None:
        handle.flush()
        os.fsync(handle.fileno())

    def promote(self, upload_id: UUID, storage_name: str) -> None:
        os.replace(self.part_path(upload_id), self.target_path(storage_name))

    def remove_part(self, upload_id: UUID) -> None:
        self._unlink_if_exists(self.part_path(upload_id))

    def remove_media(self, storage_name: str) -> None:
        self._unlink_if_exists(self.target_path(storage_name))

    def remove_attempt_directory(self, task_id: UUID, work_directory_name: str) -> None:
        if not _valid_storage_name(task_id.hex) or not _valid_storage_name(work_directory_name):
            raise StorageFailure(errno.EINVAL, "invalid generated attempt path")
        task_directory = self.analysis / task_id.hex
        path = task_directory / work_directory_name
        analysis_root = self.analysis.resolve()
        if (
            path.parent != task_directory
            or self.analysis.is_symlink()
            or task_directory.is_symlink()
            or path.is_symlink()
            or not path.resolve(strict=False).is_relative_to(analysis_root)
        ):
            raise StorageFailure(errno.EINVAL, "invalid generated attempt path")
        if not path.exists():
            return
        for root, directories, files in os.walk(path, topdown=False, followlinks=False):
            root_path = Path(root)
            for name in files:
                candidate = root_path / name
                if candidate.is_symlink():
                    candidate.unlink(missing_ok=True)
                else:
                    candidate.unlink(missing_ok=True)
            for name in directories:
                candidate = root_path / name
                if candidate.is_symlink():
                    candidate.unlink(missing_ok=True)
                else:
                    try:
                        candidate.rmdir()
                    except FileNotFoundError:
                        pass
            try:
                root_path.rmdir()
            except FileNotFoundError:
                pass
        try:
            task_directory.rmdir()
        except (FileNotFoundError, OSError):
            pass

    def remove_lease(self, upload_id: UUID) -> None:
        self._unlink_if_exists(self.lease_path(upload_id))

    @staticmethod
    def _unlink_if_exists(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return


def _valid_storage_name(name: str) -> bool:
    return len(name) == 32 and all(char in "0123456789abcdef" for char in name)
