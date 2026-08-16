"""Stream a BagIt archive straight into a zip, without staging a second copy.

The output validates as a BagIt 0.97 bag: unzip it and `bagit.Bag(dir).validate()`
passes.
"""

import hashlib
import zipfile
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

_CHUNK = 1024 * 1024

# (source_path, rel_path) — rel_path is the file's location under data/
PayloadFile = Tuple[Path, str]
# (bytes, rel_path) — for generated sidecars (metadata.json, manifest.jsonl)
PayloadBlob = Tuple[bytes, str]


def _manifest_lines(entries: List[Tuple[str, str]]) -> str:
    # BagIt manifest line: "<checksum>  <path-relative-to-bag>"
    return "".join(f"{checksum}  {path}\n" for path, checksum in entries)


def write_bag_zip(
    zip_path: Path,
    bag_name: str,
    bag_info: dict,
    payload_files: Iterable[PayloadFile],
    payload_blobs: Iterable[PayloadBlob] = (),
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Write a BagIt bag as a zip at zip_path, reading each source once.
    Returns a small summary (file_count, total_bytes). progress_cb(done, total) is called after each payload item if given.
    """
    payload_files = list(payload_files)
    payload_blobs = list(payload_blobs)
    total = len(payload_files) + len(payload_blobs)
    done = 0

    sha_entries: List[Tuple[str, str]] = []  # (bag-relative path, sha256)
    md5_entries: List[Tuple[str, str]] = []
    total_bytes = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for source_path, rel in payload_files:
            rel = rel.replace("\\", "/")
            sha, md5 = hashlib.sha256(), hashlib.md5()
            size = 0
            with open(source_path, "rb") as src, zf.open(f"{bag_name}/data/{rel}", "w") as dst:
                for chunk in iter(lambda: src.read(_CHUNK), b""):
                    dst.write(chunk)
                    sha.update(chunk)
                    md5.update(chunk)
                    size += len(chunk)
            sha_entries.append((f"data/{rel}", sha.hexdigest()))
            md5_entries.append((f"data/{rel}", md5.hexdigest()))
            total_bytes += size
            done += 1
            if progress_cb:
                progress_cb(done, total)

        for data, rel in payload_blobs:
            rel = rel.replace("\\", "/")
            zf.writestr(f"{bag_name}/data/{rel}", data)
            sha_entries.append((f"data/{rel}", hashlib.sha256(data).hexdigest()))
            md5_entries.append((f"data/{rel}", hashlib.md5(data).hexdigest()))
            total_bytes += len(data)
            done += 1
            if progress_cb:
                progress_cb(done, total)

        # Tag files. Payload-Oxum lets a validator check the payload byte/file totals
        # without reading the files, so include it.
        info = dict(bag_info)
        info["Payload-Oxum"] = f"{total_bytes}.{len(sha_entries)}"
        tag_files = {
            "bagit.txt": "BagIt-Version: 0.97\nTag-File-Character-Encoding: UTF-8\n",
            "bag-info.txt": "".join(f"{k}: {v}\n" for k, v in info.items()),
            "manifest-sha256.txt": _manifest_lines(sha_entries),
            "manifest-md5.txt": _manifest_lines(md5_entries),
        }
        for name, content in tag_files.items():
            zf.writestr(f"{bag_name}/{name}", content)

        # Tag manifests hash the tag files above (not themselves).
        tag_sha = "".join(
            f"{hashlib.sha256(c.encode('utf-8')).hexdigest()}  {n}\n" for n, c in tag_files.items()
        )
        tag_md5 = "".join(
            f"{hashlib.md5(c.encode('utf-8')).hexdigest()}  {n}\n" for n, c in tag_files.items()
        )
        zf.writestr(f"{bag_name}/tagmanifest-sha256.txt", tag_sha)
        zf.writestr(f"{bag_name}/tagmanifest-md5.txt", tag_md5)

    return {"file_count": len(sha_entries), "total_bytes": total_bytes}
