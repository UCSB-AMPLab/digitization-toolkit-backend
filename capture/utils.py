import hashlib
import os
import logging
from pathlib import Path
from typing import Callable
from logging.handlers import RotatingFileHandler


def _fsync_file(path) -> None:
    """Flush a file's bytes to disk. O_RDWR so fsync works on Windows too."""
    fd = os.open(str(path), os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(directory) -> None:
    """Flush a directory entry so a rename is durable (POSIX; no-op on Windows)."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return  # opening a directory fd is not supported on Windows
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(final_path, write_fn: Callable[[str], None]) -> str:
    """Durably write a file: temp path, fsync, atomic replace, fsync dir.

    write_fn(tmp_path) must write the complete file to the given path. The temp
    name keeps the final extension so format-by-extension writers still work.
    On return the final bytes are on disk, so a manifest written afterwards is
    never more durable than the image it records.
    """
    final_path = Path(final_path)
    tmp_path = final_path.with_name(f"{final_path.stem}.part{final_path.suffix}")
    try:
        write_fn(str(tmp_path))
        _fsync_file(tmp_path)
        os.replace(str(tmp_path), str(final_path))
        _fsync_dir(final_path.parent)
    except BaseException:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise
    return str(final_path)


def compute_sha256(file_path: str) -> str:
    """
    Compute SHA256 hash of a file.
    
    Reference: https://github.com/github-copilot/code_referencing?cursor=20401bb2b76e5586f3eb23414fa0a226
    Args:
        file_path: Path to the file.
    Returns:
        SHA256 hash as a hexadecimal string.
    """
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def setup_rotating_logger(log_file: str, logger_name: str, level=logging.INFO, max_bytes=5*1024*1024, backup_count=5) -> logging.Logger:
    """
    Set up a rotating file logger.
    
    Args:
        log_file: Path to the log file.
        logger_name: Name of the logger.
        level: Logging level.
        max_bytes: Maximum size of a log file before rotation.
        backup_count: Number of backup log files to keep.
        
    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    # Guard against duplicate handlers when the module is reimported by
    # uvicorn's auto-reloader (the logger singleton persists across reloads).
    if not any(
        isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == log_file
        for h in logger.handlers
    ):
        handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger