"""
Saksham AI - Documents API
Handles file uploads and text extraction.
"""

from fastapi import APIRouter, UploadFile, File, HTTPException
from core.document_processor import document_processor
from typing import Dict

router = APIRouter()

@router.post("/upload")
async def upload_document(file: UploadFile = File(...)) -> Dict[str, str]:
    """
    Upload a file and get its text content.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    if not document_processor.is_supported(file.filename):
        raise HTTPException(
            status_code=400, 
            detail=f"Unsupported file type. Supported: {', '.join(document_processor.supported_extensions.keys())}"
        )

    try:
        content = await file.read()
        
        # Limit file size (e.g., 5MB)
        if len(content) > 5 * 1024 * 1024:
             raise HTTPException(status_code=400, detail="File too large (Max 5MB)")

        extracted_text = await document_processor.process(file.filename, content)
        
        return {
            "filename": file.filename,
            "text": extracted_text,
            "type": "file_content"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
