import hashlib

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