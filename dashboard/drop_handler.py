"""
Drop Handler — processes files/folders/photos/links dropped onto the dashboard.

Routes dropped items by extension to the appropriate handler:
  - Images → vision analysis via llava
  - Text/code files → Brain summarize
  - PDF → extract text + summarize
  - Directories → list contents and describe
"""

import logging
import os

logger = logging.getLogger(__name__)

# Extension → category mapping
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff"}
CODE_EXTS = {".py", ".js", ".ts", ".html", ".css", ".java", ".cpp", ".c",
             ".h", ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1"}
TEXT_EXTS = {".txt", ".md", ".log", ".csv", ".json", ".xml", ".yaml", ".yml",
             ".ini", ".cfg", ".toml"}
DOC_EXTS = {".pdf", ".doc", ".docx"}


class DropHandler:
    """Processes dropped files and sends results to the chat."""

    def __init__(self, brain_thread):
        self._brain = brain_thread

    def process(self, path: str, emit_fn):
        """
        Process a dropped file/folder.

        Args:
            path: File or folder path
            emit_fn: Callback(role, text) to send messages to chat
        """
        path = path.strip().strip('"').strip("'")

        if not os.path.exists(path):
            emit_fn("system", f"File not found: {path}")
            return

        if os.path.isdir(path):
            self._handle_directory(path, emit_fn)
        else:
            ext = os.path.splitext(path)[1].lower()
            if ext in IMAGE_EXTS:
                self._handle_image(path, emit_fn)
            elif ext in CODE_EXTS or ext in TEXT_EXTS:
                self._handle_text(path, emit_fn)
            elif ext in DOC_EXTS:
                self._handle_document(path, emit_fn)
            else:
                self._handle_generic(path, emit_fn)

    def _handle_image(self, path: str, emit_fn):
        """Analyze image via vision/llava."""
        emit_fn("system", f"Analyzing image: {os.path.basename(path)}...")
        try:
            from vision import analyze_screenshot_with_llava, image_to_base64
            from PIL import Image
            img = Image.open(path)
            b64 = image_to_base64(img)
            description = analyze_screenshot_with_llava(
                b64, "Describe this image in detail. What do you see?"
            )
            emit_fn("assistant", f"**{os.path.basename(path)}**: {description}")
        except Exception as e:
            logger.error(f"Image analysis failed: {e}")
            emit_fn("assistant", f"I can see you dropped an image ({os.path.basename(path)}), "
                     f"but vision analysis isn't available right now.")

    def _handle_text(self, path: str, emit_fn):
        """Read and summarize text/code files."""
        emit_fn("system", f"Reading: {os.path.basename(path)}...")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(50_000)  # Cap at 50KB

            lines = content.count("\n") + 1
            ext = os.path.splitext(path)[1]
            kind = "code" if ext in CODE_EXTS else "text"

            # Send to brain for summarization
            prompt = (f"The user dropped a {kind} file: {os.path.basename(path)} "
                      f"({lines} lines). Summarize its contents:\n\n{content[:5000]}")
            self._brain.enqueue(prompt)

        except Exception as e:
            emit_fn("system", f"Could not read {os.path.basename(path)}: {e}")

    def _handle_document(self, path: str, emit_fn):
        """Handle PDF and document files."""
        emit_fn("system", f"Processing document: {os.path.basename(path)}...")

        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            try:
                # Try PyPDF2 or pdfplumber if available
                text = self._extract_pdf_text(path)
                if text:
                    prompt = (f"The user dropped a PDF: {os.path.basename(path)}. "
                              f"Summarize its contents:\n\n{text[:5000]}")
                    self._brain.enqueue(prompt)
                else:
                    emit_fn("assistant", f"I received the PDF {os.path.basename(path)} "
                            f"but couldn't extract text from it.")
            except Exception as e:
                emit_fn("system", f"PDF processing failed: {e}")
        else:
            emit_fn("assistant", f"I received {os.path.basename(path)}. "
                     f"I can currently process .txt, .py, .pdf and image files.")

    def _handle_directory(self, path: str, emit_fn):
        """List directory contents."""
        emit_fn("system", f"Scanning directory: {os.path.basename(path)}...")
        try:
            entries = os.listdir(path)
            dirs = [e for e in entries if os.path.isdir(os.path.join(path, e))]
            files = [e for e in entries if os.path.isfile(os.path.join(path, e))]

            summary = f"**{os.path.basename(path)}/** — {len(dirs)} folders, {len(files)} files\n"
            if dirs:
                summary += f"\nFolders: {', '.join(dirs[:15])}"
                if len(dirs) > 15:
                    summary += f" ... +{len(dirs) - 15} more"
            if files:
                summary += f"\nFiles: {', '.join(files[:20])}"
                if len(files) > 20:
                    summary += f" ... +{len(files) - 20} more"

            emit_fn("assistant", summary)
        except Exception as e:
            emit_fn("system", f"Could not read directory: {e}")

    def _handle_generic(self, path: str, emit_fn):
        """Handle unknown file types."""
        size = os.path.getsize(path)
        size_str = (f"{size} bytes" if size < 1024
                    else f"{size / 1024:.1f} KB" if size < 1024 * 1024
                    else f"{size / (1024 * 1024):.1f} MB")
        emit_fn("assistant", f"Received **{os.path.basename(path)}** ({size_str}). "
                f"I'm not sure how to process this file type.")

    @staticmethod
    def _extract_pdf_text(path: str) -> str | None:
        """Try to extract text from a PDF."""
        # Try PyPDF2
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(path)
            text = ""
            for page in reader.pages[:20]:  # Cap at 20 pages
                text += page.extract_text() or ""
            return text.strip() if text.strip() else None
        except ImportError:
            pass

        # Try pdfplumber
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                text = ""
                for page in pdf.pages[:20]:
                    text += page.extract_text() or ""
            return text.strip() if text.strip() else None
        except ImportError:
            pass

        return None
