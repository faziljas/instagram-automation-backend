"""
Upload routes for automation media (video/image/card).
Same approach as Report an Issue: receive file via multipart, but we save and serve it
so the returned URL can be used in Primary DMs later.
"""
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Request
from app.dependencies.auth import get_current_user_id

router = APIRouter()

# Limit 20MB for video/image (Instagram DM media)
MAX_FILE_SIZE = 20 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ALLOWED_VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm"}


def get_uploads_dir() -> Path:
    """Return the uploads directory, creating it if needed."""
    # Prefer env so deployment can set a persistent volume path
    base = os.getenv("UPLOADS_DIR")
    if base:
        d = Path(base)
    else:
        d = Path(__file__).resolve().parent.parent.parent.parent / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.post("/upload/media")
async def upload_media(
    request: Request,
    file: UploadFile = File(...),
    user_id: int = Depends(get_current_user_id),
):
    """
    Upload a video or image for automation Primary DM media.
    Saves file to backend and returns a public URL (same host as API).
    No Supabase required.
    """
    content = await file.read()
    size = len(content)
    if size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File exceeds maximum size of 20MB",
        )

    content_type = (file.content_type or "").strip().lower()
    if content_type in ALLOWED_IMAGE_TYPES:
        subdir = "images"
    elif content_type in ALLOWED_VIDEO_TYPES:
        subdir = "videos"
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Allowed: image (jpeg, png, gif, webp) or video (mp4, mov, webm)",
        )

    # Safe filename: keep extension, unique prefix
    ext = Path(file.filename or "file").suffix or (".jpg" if "image" in content_type else ".mp4")
    safe_name = f"{uuid.uuid4().hex[:12]}{ext}"
    uploads_dir = get_uploads_dir()
    target_dir = uploads_dir / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name

    try:
        target_path.write_bytes(content)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save file: {str(e)}",
        )

    # Public URL for the uploaded file (so Primary DM can use it later)
    base_url = os.getenv("PUBLIC_API_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    path_segment = f"uploads/{subdir}/{safe_name}"
    public_url = f"{base_url}/{path_segment}"

    return {"url": public_url}

