from __future__ import annotations

# 本模块封装本地媒体与分析文件的受控路径、跨进程写入租约和清理操作。
import errno
import hashlib
import importlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID

RESUMABLE_CHUNK_SIZE = 8 * 1024 * 1024


class StorageFailure(OSError):
    """A bounded, path-free storage error suitable for API diagnostics."""


@dataclass
# 跨进程写入租约保护同一上传文件，调用方必须持有租约才能修改分片。
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


# 媒体存储统一管理临时分片、正式文件、路径校验和清理操作。
class MediaStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.incoming = self.root / "incoming"
        self.media = self.root / "media"
        self.leases = self.root / "leases"
        self.analysis = self.root / "analysis"
        self._verified_upload_prefixes: dict[UUID, tuple[int, int, int]] = {}

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
        view = memoryview(chunk)
        while view:
            written = handle.write(view)
            if written is None or written <= 0:
                raise StorageFailure(errno.EIO, "short write while storing upload data")
            view = view[written:]

    @staticmethod
    # 确认分片前先刷新用户态缓冲并同步到磁盘，数据库偏移不会领先于已落盘数据。
    def flush_part(handle: BinaryIO) -> None:
        handle.flush()
        os.fsync(handle.fileno())

    # 只保留数据库确认范围内且摘要匹配的内容，未确认尾部会被截断或重建。
    def reconcile_resumable_part(
        self,
        upload_id: UUID,
        confirmed_offset: int,
        chunks: Sequence[tuple[int, int, int, str]],
    ) -> None:
        """Verify confirmed bytes and truncate any uncommitted file tail."""
        path = self.part_path(upload_id)
        try:
            if not path.exists():
                if confirmed_offset:
                    raise StorageFailure(errno.EIO, "confirmed upload file is missing")
                with path.open("xb", buffering=0) as created:
                    self.flush_part(created)
            if not path.is_file():
                raise StorageFailure(errno.EIO, "confirmed upload file is not a regular file")
            stat = path.stat()
            if stat.st_size < confirmed_offset:
                raise StorageFailure(errno.EIO, "upload file is shorter than confirmed offset")

            expected_offset = 0
            for expected_index, (chunk_index, offset, length, _digest_value) in enumerate(chunks):
                if (
                    chunk_index != expected_index
                    or offset != expected_offset
                    or offset != expected_index * RESUMABLE_CHUNK_SIZE
                    or length <= 0
                    or expected_offset + length > confirmed_offset
                ):
                    raise StorageFailure(errno.EIO, "confirmed upload chunk manifest is discontinuous")
                expected_offset += length
            if expected_offset != confirmed_offset:
                raise StorageFailure(errno.EIO, "confirmed upload manifest does not match offset")

            # 只有文件长度、修改时间和确认偏移均匹配时才复用前缀摘要，否则逐片重算。
            cached = self._verified_upload_prefixes.get(upload_id)
            cache_matches = (
                cached is not None
                and stat.st_size == confirmed_offset
                and cached == (stat.st_size, stat.st_mtime_ns, confirmed_offset)
            )
            if not cache_matches and confirmed_offset:
                with path.open("rb", buffering=0) as source:
                    for _, offset, length, expected_digest in chunks:
                        source.seek(offset)
                        digest = hashlib.sha256()
                        remaining = length
                        while remaining:
                            block = source.read(min(1024 * 1024, remaining))
                            if not block:
                                raise StorageFailure(errno.EIO, "confirmed upload data is truncated")
                            digest.update(block)
                            remaining -= len(block)
                        if digest.hexdigest() != expected_digest:
                            raise StorageFailure(errno.EIO, "confirmed upload digest does not match")

            if stat.st_size != confirmed_offset:
                with path.open("r+b", buffering=0) as writable:
                    writable.truncate(confirmed_offset)
                    self.flush_part(writable)
                stat = path.stat()
            self._verified_upload_prefixes[upload_id] = (
                stat.st_size,
                stat.st_mtime_ns,
                confirmed_offset,
            )
        except StorageFailure:
            raise
        except OSError as exc:
            raise StorageFailure(exc.errno or errno.EIO, "could not reconcile resumable upload data") from exc

    def open_part_at_offset(self, upload_id: UUID, offset: int) -> BinaryIO:
        path = self.part_path(upload_id)
        try:
            handle = path.open("r+b", buffering=0)
            handle.seek(offset)
            return handle
        except OSError as exc:
            raise StorageFailure(exc.errno or errno.EIO, "could not open resumable upload data") from exc

    def file_sha256(self, upload_id: UUID) -> tuple[int, str]:
        return self._sha256_path(self.part_path(upload_id))

    def media_sha256(self, storage_name: str) -> tuple[int, str]:
        return self._sha256_path(self.target_path(storage_name))

    def part_exists(self, upload_id: UUID) -> bool:
        return self.part_path(upload_id).is_file()

    def media_exists(self, storage_name: str) -> bool:
        return self.target_path(storage_name).is_file()

    @staticmethod
    def _sha256_path(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        byte_count = 0
        try:
            with path.open("rb", buffering=0) as source:
                while block := source.read(1024 * 1024):
                    byte_count += len(block)
                    digest.update(block)
        except OSError as exc:
            raise StorageFailure(exc.errno or errno.EIO, "could not hash resumable upload data") from exc
        return byte_count, digest.hexdigest()

    def remember_resumable_prefix(self, upload_id: UUID, confirmed_offset: int) -> None:
        path = self.part_path(upload_id)
        try:
            stat = path.stat()
        except OSError as exc:
            self._verified_upload_prefixes.pop(upload_id, None)
            raise StorageFailure(exc.errno or errno.EIO, "could not verify committed upload data") from exc
        if stat.st_size != confirmed_offset:
            self._verified_upload_prefixes.pop(upload_id, None)
            raise StorageFailure(errno.EIO, "committed upload size does not match confirmed offset")
        self._verified_upload_prefixes[upload_id] = (
            stat.st_size,
            stat.st_mtime_ns,
            confirmed_offset,
        )

    def promote(self, upload_id: UUID, storage_name: str) -> None:
        # 用文件名替换完成临时分片到正式媒体路径的交接，避免复制期间暴露半成品。
        os.replace(self.part_path(upload_id), self.target_path(storage_name))
        self._verified_upload_prefixes.pop(upload_id, None)

    def remove_part(self, upload_id: UUID) -> None:
        self._unlink_if_exists(self.part_path(upload_id))
        self._verified_upload_prefixes.pop(upload_id, None)

    def remove_media(self, storage_name: str) -> None:
        self._unlink_if_exists(self.target_path(storage_name))
        try:
            self._verified_upload_prefixes.pop(UUID(hex=storage_name), None)
        except ValueError:
            return

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
        # 遍历时不进入符号链接目录，清理范围限制在已校验过的本次尝试目录内。
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
        # Keep the inode stable: unlinking an advisory lock after release can let
        # another process lock the old inode while a third process locks a new one.
        _ = upload_id

    @staticmethod
    def _unlink_if_exists(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return


def _valid_storage_name(name: str) -> bool:
    return len(name) == 32 and all(char in "0123456789abcdef" for char in name)
