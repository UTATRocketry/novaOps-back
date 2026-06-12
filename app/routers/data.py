from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.context import AppContext
from app.deps import get_context

router = APIRouter(prefix="/api/data-files", tags=["Data"])


@router.get("", summary="List CSV files", description="List available CSV files from local data directory.")
async def list_data_files(ctx: AppContext = Depends(get_context)) -> list[str]:
    return sorted(path.name for path in ctx.data_dir.glob("*.csv"))


@router.get("/{file_name}", summary="Download CSV file", description="Download a CSV file by file name.")
async def get_data_file(file_name: str, ctx: AppContext = Depends(get_context)) -> FileResponse:
    file_path = ctx.data_dir / file_name
    if not file_path.exists() or file_path.suffix.lower() != ".csv":
        raise HTTPException(status_code=404, detail="CSV file not found")
    return FileResponse(file_path)


@router.post("/upload", summary="Upload data file", description="Upload a datafile.")
async def upload_data_file(file: UploadFile = File(...), ctx: AppContext = Depends(get_context)) -> dict:
    file_name = Path(file.filename or "").name
    if not file_name:
        raise HTTPException(status_code=400, detail="File name is required")

    payload = await file.read()
    target_path = ctx.data_dir / file_name
    target_path.write_bytes(payload)
    return {"file_name": file_name, "bytes_written": len(payload)}
