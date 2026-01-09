"""
Thumbnail generation utilities for document images.

This module provides functions for generating and managing thumbnails of document images.
"""

import logging
from pathlib import Path
from typing import Optional
from PIL import Image
import uuid

logger = logging.getLogger(__name__)

# Default thumbnail dimensions
DEFAULT_THUMBNAIL_WIDTH = 200
DEFAULT_THUMBNAIL_HEIGHT = 200
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
        # Open the image
        with Image.open(source_path) as img:
            # Convert RGBA/P to RGB for JPEG compatibility
            if img.mode in ("RGBA", "P", "LA"):
                # Create white background
                rgb_img = Image.new("RGB", img.size, (255, 255, 255))
                rgb_img.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = rgb_img
            
            # Calculate thumbnail size maintaining aspect ratio
            img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
            
            # Generate unique filename for thumbnail
            thumb_filename = f"{uuid.uuid4().hex}_thumb.jpg"
            thumb_path = dest_dir / thumb_filename
            
            # Save thumbnail
            img.save(thumb_path, format=DEFAULT_THUMBNAIL_FORMAT, quality=quality, optimize=True)
            
            logger.info(f"Generated thumbnail: {thumb_path}")
            return str(thumb_path)
    
    except Exception as e:
        logger.error(f"Failed to generate thumbnail for {source_path}: {e}")
        return None


def delete_thumbnail(thumbnail_path: Optional[str]) -> bool:
    """
    Delete a thumbnail file.
    
    Args:
        thumbnail_path: Path to the thumbnail file
    
    Returns:
        True if deletion was successful or file didn't exist, False on error
    """
    if not thumbnail_path:
        return True
    
    try:
        thumb_file = Path(thumbnail_path)
        if thumb_file.exists():
            thumb_file.unlink()
            logger.info(f"Deleted thumbnail: {thumbnail_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to delete thumbnail {thumbnail_path}: {e}")
        return False


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
    """
    # Delete old thumbnail
    delete_thumbnail(old_thumbnail_path)
    
    # Generate new thumbnail
    return generate_thumbnail(source_path, dest_dir, max_width, max_height, quality)
