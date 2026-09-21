"""
Saksham AI - File Controller
File system operations with safety checks.
"""

import os
import shutil
from pathlib import Path
from typing import Optional

from loguru import logger


class FileController:
    """
    Handles file system operations for Saksham AI.
    Includes safety checks to prevent accidental damage.
    """
    
    # Protected system paths that should never be modified
    PROTECTED_PATHS = {
        "/",
        "/System",
        "/Library",
        "/usr",
        "/bin",
        "/sbin",
        "/private",
        "/var",
        "/etc",
    }
    
    def __init__(self):
        self.home = Path.home()
    
    def _is_protected(self, path: str) -> bool:
        """Check if path is protected"""
        path_obj = Path(path).resolve()
        
        for protected in self.PROTECTED_PATHS:
            if str(path_obj) == protected or str(path_obj).startswith(protected + "/"):
                # Allow within user's home
                if str(path_obj).startswith(str(self.home)):
                    return False
                return True
        return False
    
    def _validate_path(self, path: str) -> Path:
        """Validate and resolve path"""
        path_obj = Path(path).expanduser().resolve()
        
        if self._is_protected(str(path_obj)):
            raise PermissionError(f"Path is protected: {path}")
        
        return path_obj
    
    async def read(self, path: str, encoding: str = "utf-8") -> dict:
        """
        Read file contents.
        
        Args:
            path: Path to the file
            encoding: File encoding (default: utf-8)
        """
        try:
            file_path = self._validate_path(path)
            
            if not file_path.exists():
                return {"success": False, "error": "File not found"}
            
            if not file_path.is_file():
                return {"success": False, "error": "Path is not a file"}
            
            content = file_path.read_text(encoding=encoding)
            
            return {
                "success": True,
                "path": str(file_path),
                "content": content,
                "size": len(content),
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def write(
        self,
        path: str,
        content: str,
        encoding: str = "utf-8",
        create_dirs: bool = True,
    ) -> dict:
        """
        Write content to a file.
        
        Args:
            path: Path to the file
            content: Content to write
            encoding: File encoding
            create_dirs: Create parent directories if needed
        """
        try:
            file_path = self._validate_path(path)
            
            if create_dirs:
                file_path.parent.mkdir(parents=True, exist_ok=True)
            
            file_path.write_text(content, encoding=encoding)
            
            return {
                "success": True,
                "path": str(file_path),
                "size": len(content),
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def create(
        self,
        path: str,
        is_directory: bool = False,
        content: str = "",
    ) -> dict:
        """
        Create a file or directory.
        """
        try:
            file_path = self._validate_path(path)
            
            if is_directory:
                file_path.mkdir(parents=True, exist_ok=True)
            else:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content)
            
            return {
                "success": True,
                "path": str(file_path),
                "is_directory": is_directory,
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def delete(
        self,
        path: str,
        recursive: bool = False,
    ) -> dict:
        """
        Delete a file or directory.
        
        Args:
            path: Path to delete
            recursive: If True, delete directories recursively
        """
        try:
            file_path = self._validate_path(path)
            
            if not file_path.exists():
                return {"success": False, "error": "Path not found"}
            
            if file_path.is_dir():
                if recursive:
                    shutil.rmtree(file_path)
                else:
                    file_path.rmdir()
            else:
                file_path.unlink()
            
            return {
                "success": True,
                "deleted": str(file_path),
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def move(self, source: str, destination: str) -> dict:
        """Move a file or directory"""
        try:
            src_path = self._validate_path(source)
            dst_path = self._validate_path(destination)
            
            shutil.move(str(src_path), str(dst_path))
            
            return {
                "success": True,
                "source": str(src_path),
                "destination": str(dst_path),
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def copy(self, source: str, destination: str) -> dict:
        """Copy a file or directory"""
        try:
            src_path = self._validate_path(source)
            dst_path = self._validate_path(destination)
            
            if src_path.is_dir():
                shutil.copytree(str(src_path), str(dst_path))
            else:
                shutil.copy2(str(src_path), str(dst_path))
            
            return {
                "success": True,
                "source": str(src_path),
                "destination": str(dst_path),
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def list_directory(
        self,
        path: str,
        pattern: str = "*",
        recursive: bool = False,
    ) -> dict:
        """
        List contents of a directory.
        """
        try:
            dir_path = self._validate_path(path)
            
            if not dir_path.is_dir():
                return {"success": False, "error": "Path is not a directory"}
            
            if recursive:
                items = list(dir_path.rglob(pattern))
            else:
                items = list(dir_path.glob(pattern))
            
            contents = []
            for item in items:
                contents.append({
                    "name": item.name,
                    "path": str(item),
                    "is_directory": item.is_dir(),
                    "size": item.stat().st_size if item.is_file() else None,
                })
            
            return {
                "success": True,
                "path": str(dir_path),
                "contents": contents,
                "count": len(contents),
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def reveal_in_finder(self, path: str) -> dict:
        """Open path in Finder"""
        from .applescript_bridge import run_applescript, AppleScriptTemplates
        
        try:
            file_path = self._validate_path(path)
            await run_applescript(AppleScriptTemplates.reveal_in_finder(str(file_path)))
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
