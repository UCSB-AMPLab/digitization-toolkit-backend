import hashlib
import logging
from logging.handlers import RotatingFileHandler

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
    
    handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    
    logger.addHandler(handler)
    return logger