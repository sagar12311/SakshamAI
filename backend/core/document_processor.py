"""
Saksham AI - Document Processor
Extracts text from various file formats for LLM analysis.
"""

from pathlib import Path
from io import BytesIO
from loguru import logger
import pypdf

class DocumentProcessor:
    def __init__(self):
        self.supported_extensions = {
            # Code
            '.py': 'python',
            '.js': 'javascript',
            '.ts': 'typescript',
            '.tsx': 'typescript-react',
            '.html': 'html',
            '.css': 'css',
            '.json': 'json',
            '.md': 'markdown',
            '.sql': 'sql',
            '.sh': 'shell',
            '.yaml': 'yaml',
            '.yml': 'yaml',
            '.xml': 'xml',
            '.java': 'java',
            '.c': 'c',
            '.cpp': 'cpp',
            
            # Documents
            '.txt': 'text',
            '.pdf': 'pdf',
        }

    def is_supported(self, filename: str) -> bool:
        ext = Path(filename).suffix.lower()
        return ext in self.supported_extensions

    async def process(self, filename: str, content: bytes) -> str:
        """Extract text content from file bytes"""
        ext = Path(filename).suffix.lower()
        
        if ext == '.pdf':
            return self._process_pdf(content)
        else:
            # Assume text/code for other supported extensions
            return self._process_text(content, filename)

    def _process_text(self, content: bytes, filename: str) -> str:
        try:
            # Try parsing as UTF-8
            text = content.decode('utf-8')
            return f"--- FILE START: {filename} ---\n{text}\n--- FILE END ---\n"
        except UnicodeDecodeError:
            try:
                # Fallback to latin-1
                text = content.decode('latin-1')
                return f"--- FILE START: {filename} (latin-1) ---\n{text}\n--- FILE END ---\n"
            except Exception as e:
                logger.error(f"Failed to decode text file {filename}: {e}")
                return f"[Error: Could not decode text file {filename}]"

    def _process_pdf(self, content: bytes) -> str:
        try:
            pdf_file = BytesIO(content)
            reader = pypdf.PdfReader(pdf_file)
            text = []
            
            for i, page in enumerate(reader.pages):
                page_text = page.extract_text()
                if page_text:
                    text.append(f"--- Page {i+1} ---\n{page_text}")
            
            full_text = "\n".join(text)
            return f"--- PDF START ---\n{full_text}\n--- PDF END ---\n"
            
        except Exception as e:
            logger.error(f"Failed to process PDF: {e}")
            return f"[Error: Could not parse PDF file: {str(e)}]"

# Global instance
document_processor = DocumentProcessor()
