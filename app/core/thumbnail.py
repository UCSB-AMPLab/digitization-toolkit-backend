"""
Thumbnail generation utilities for record images.

This module provides functions for generating and managing thumbnails of record images.
"""

import logging
from pathlib import Path
from typing import Optional
from PIL import Image, ImageOps
import uuid

logger = logging.getLogger(__name__)

# 600px covers the largest grid-view cell size in the frontend (GridView's
# column-count slider goes down to 2 columns, which can render cells ~500-
# 540px wide) with a bit of margin. A smaller thumbnail is upscaled by
# `object-fit: cover` to fill those cells, which blurs the grid. The size
# applies to the thumbnail only, not the stored master image, so its storage
# and bandwidth cost stays a small fraction of the full-resolution capture.
DEFAULT_THUMBNAIL_WIDTH = 600
DEFAULT_THUMBNAIL_HEIGHT = 600
DEFAULT_THUMBNAIL_FORMAT = "JPEG"
DEFAULT_THUMBNAIL_QUALITY = 85


def generate_thumbnail(
    source_path: Path,
    dest_dir: Path,
    max_width: int = DEFAULT_THUMBNAIL_WIDTH,
    max_height: int = DEFAULT_THUMBNAIL_HEIGHT,
    quality: int = DEFAULT_THUMBNAIL_QUALITY,
) -> Optional[str]:
    """
    Generate a thumbnail for an image file.
    
    Args:
        source_path: Path to the source image file
        dest_dir: Directory to save the thumbnail
        max_width: Maximum width of the thumbnail in pixels
        max_height: Maximum height of the thumbnail in pixels
        quality: JPEG quality (1-100)
    
    Returns:
        Path to the generated thumbnail as a string, or None if generation failed
    
    Raises:
        FileNotFoundError: If source file doesn't exist
    """
    if not source_path.exists():
        raise FileNotFoundError(f"Source image not found: {source_path}")
    
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        with Image.open(source_path) as img:
            # Honour EXIF Orientation so a rotated master thumbnails upright
            img = ImageOps.exif_transpose(img)
            # Convert RGBA/P to RGB for JPEG compatibility
            if img.mode in ("RGBA", "P", "LA"):
                rgb_img = Image.new("RGB", img.size, (255, 255, 255))
                rgb_img.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = rgb_img
            
            # Calculate thumbnail size maintaining aspect ratio
            img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
            
            thumb_filename = f"{uuid.uuid4().hex}_thumb.jpg"
            thumb_path = dest_dir / thumb_filename
            
            img.save(thumb_path, format=DEFAULT_THUMBNAIL_FORMAT, quality=quality, optimize=True)
            
            logger.info(f"Generated thumbnail: {thumb_path}")
            return str(thumb_path)
    
    except Exception as e:
        logger.error(f"Failed to generate thumbnail for {source_path}: {e}")
        return None


def delete_thumbnail(thumbnail_path: Optional[str]) -> None:
    """
    Delete a thumbnail file. An empty path or a file already gone is a no-op.

    An OSError from the unlink propagates (NEH-263), so a thumbnail that cannot
    be removed is never reported as removed, and each caller decides what the
    failure means where it happens.

    Args:
        thumbnail_path: Path to the thumbnail file

    Raises:
        OSError: if the file exists and cannot be unlinked
    """
    if not thumbnail_path:
        return
    Path(thumbnail_path).unlink(missing_ok=True)
    logger.info(f"Deleted thumbnail: {thumbnail_path}")


def regenerate_thumbnail(
    source_path: Path,
    old_thumbnail_path: Optional[str],
    dest_dir: Path,
    max_width: int = DEFAULT_THUMBNAIL_WIDTH,
    max_height: int = DEFAULT_THUMBNAIL_HEIGHT,
    quality: int = DEFAULT_THUMBNAIL_QUALITY,
) -> Optional[str]:
    """
    Regenerate a thumbnail by deleting the old one and creating a new one.
    
    Args:
        source_path: Path to the source image file
        old_thumbnail_path: Path to the old thumbnail to delete
        dest_dir: Directory to save the new thumbnail
        max_width: Maximum width of the thumbnail
        max_height: Maximum height of the thumbnail
        quality: JPEG quality
    
    Returns:
        Path to the new thumbnail, or None if generation failed

    Raises:
        OSError: if the old thumbnail exists and cannot be unlinked; no new
            thumbnail is generated in that case
    """
    delete_thumbnail(old_thumbnail_path)
    
    return generate_thumbnail(source_path, dest_dir, max_width, max_height, quality)
