"""
Upload routes for DM automation media (image, video, voice).
Files are stored under uploads/dm-media/{user_id}/ and served via the same API.
"""
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from app.dependencies.auth import get_current_user_id

router = APIRouter()

# Base directory for uploads (relative to project root)
UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent.parent / "uploads" / "dm-media"
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25MB for video/audio
ALLOWED_IMAGE_VIDEO = {"image/jpeg", "image/png", "image/gif", "image/webp", "video/mp4", "video/quicktime", "video/webm"}
ALLOWED_AUDIO = {"audio/mpeg", "audio/mp3", "audio/mp4", "audio/ogg", "audio/wav", "audio/webm", "audio/x-m4a"}


def _get_content_type(file: UploadFile) -> str:
    ct = (file.content_type or "").strip().lower()
    if ct:
        return ct
    # Fallback from filename
    name = (file.filename or "").lower()
    if name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".gif"):
        return "image/gif"
    if name.endswith(".webp"):
        return "image/webp"
    if name.endswith(".mp4"):
        return "video/mp4"
    if name.endswith(".mov"):
        return "video/quicktime"
    if name.endswith(".webm"):
        return "video/webm"
    if name.endswith((".mp3", ".mpeg")):
        return "audio/mpeg"
    if name.endswith((".m4a", ".mp4")):
        return "audio/mp4"
    if name.endswith(".ogg"):
        return "audio/ogg"
    if name.endswith(".wav"):
        return "audio/wav"
    return ""


@router.post("/dm-media")
async def upload_dm_media(
    file: UploadFile = File(...),
    user_id: int = Depends(get_current_user_id),
):
    """
    Upload a single file for DM automation: image, video, or audio.
    Returns a public URL that can be stored in automation config (dm_media_url or dm_voice_message_url).
    """
    if not file.filename or not file.filename.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing filename")

    content_type = _get_content_type(file)
    allowed = ALLOWED_IMAGE_VIDEO | ALLOWED_AUDIO
    if content_type not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type not allowed. Use image (JPEG/PNG/GIF/WebP), video (MP4/MOV/WebM), or audio (MP3/M4A/OGG/WAV). Got: {content_type or 'unknown'}"
        )

    # Read and check size
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File too large. Maximum size is {MAX_FILE_SIZE // (1024 * 1024)}MB."
        )

    # Save to uploads/dm-media/{user_id}/{uuid}_{sanitized_filename}
    user_dir = UPLOAD_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in file.filename if c.isalnum() or c in "._- ").strip() or "file"
    unique_name = f"{uuid.uuid4().hex}_{safe_name}"
    file_path = user_dir / unique_name
    file_path.write_bytes(content)

    # Public URL: use API base URL from env so Instagram/backend can fetch the file
    base_url = os.getenv("API_PUBLIC_URL", "").rstrip("/")
    if not base_url:
        base_url = os.getenv("BASE_URL", "").rstrip("/")
    if not base_url:
        # Fallback: NEXT_PUBLIC_API_URL (often set on Render) points to backend
        base_url = os.getenv("NEXT_PUBLIC_API_URL", "http://localhost:8000").rstrip("/").rstrip("/api")
    relative_path = f"uploads/dm-media/{user_id}/{unique_name}"
    url = f"{base_url}/{relative_path}"

    return {"url": url, "filename": file.filename}
