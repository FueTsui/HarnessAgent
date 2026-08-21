"""上传读取公共工具。"""
from fastapi import HTTPException, UploadFile, status

CHUNK_BYTES = 256 * 1024


async def read_upload_limited(upload: UploadFile, max_bytes: int, label: str = "文件") -> bytes:
    """分块读取需要在内存解析的上传文件，并在累计过程中执行硬上限。"""
    data = bytearray()
    while True:
        chunk = await upload.read(CHUNK_BYTES)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"{label}超出大小限制（最大 {max_bytes // (1024 * 1024) or 1}MB）",
            )
    return bytes(data)
