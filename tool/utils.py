import json
import os
import zipfile

from docx import Document
from pdfminer.high_level import extract_text as pdf_extract_text

DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".txt"}
CODE_EXTENSIONS = {
    ".py", ".ipynb", ".r", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".scala", ".c", ".h", ".cpp",
    ".hpp", ".cc", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".m", ".sql", ".sh", ".html", ".css",
    ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".hs", ".ml", ".lua", ".pl", ".jl", ".dart", ".vue",
}
ARCHIVE_EXTENSIONS = {".zip"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# Inside a .zip: skip dependencies, build output and tooling.
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env", ".idea", ".vscode", "dist", "build",
    "target", "bin", "obj", ".next", ".pytest_cache", ".mypy_cache", "__MACOSX", ".ipynb_checkpoints",
}
MAX_ARCHIVE_FILES = 200
MAX_ARCHIVE_FILE_BYTES = 512 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 4 * 1024 * 1024


class ExtractionError(Exception):
    pass


def allowed_extensions(code=False):
    return DOCUMENT_EXTENSIONS | CODE_EXTENSIONS | ARCHIVE_EXTENSIONS if code else DOCUMENT_EXTENSIONS


def extension(filename):
    return os.path.splitext(filename or "")[1].lower()


def _normalise(text):
    lines = (" ".join(line.split()) for line in text.replace("\xa0", " ").splitlines())
    return "\n".join(line for line in lines if line)


def _tidy_code(text):
    """Keep indentation; just normalise line endings, tabs and trailing space."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n")
    return "\n".join(line.rstrip() for line in lines).strip("\n")


def _notebook_text(raw):
    try:
        notebook = json.loads(raw)
    except json.JSONDecodeError:
        raise ExtractionError("That notebook could not be read.")
    parts = []
    for cell in notebook.get("cells", []):
        source = "".join(cell.get("source", []))
        if not source.strip():
            continue
        if cell.get("cell_type") == "code":
            parts.append(source)
        else:
            parts.append("\n".join(f"# {line}" if line else "#" for line in source.splitlines()))
    return "\n\n".join(parts)


def _code_text(name, raw_bytes):
    text = raw_bytes.decode("utf-8", errors="replace")
    if extension(name) == ".ipynb":
        text = _notebook_text(text)
    return _tidy_code(text)


def _archive_text(path):
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ExtractionError("That .zip file could not be opened.")
    parts, total, count = [], 0, 0
    with archive:
        for info in sorted(archive.infolist(), key=lambda i: i.filename):
            name = info.filename
            if info.is_dir() or any(part in SKIP_DIRS or part.startswith(".") for part in name.split("/")[:-1]):
                continue
            if extension(name) not in CODE_EXTENSIONS or info.file_size > MAX_ARCHIVE_FILE_BYTES:
                continue
            count += 1
            total += info.file_size
            if count > MAX_ARCHIVE_FILES or total > MAX_ARCHIVE_TOTAL_BYTES:
                raise ExtractionError(
                    "That project is too large to assess. Upload a .zip of just your own source files."
                )
            with archive.open(info) as f:
                code = _code_text(name, f.read(MAX_ARCHIVE_FILE_BYTES))
            if code.strip():
                parts.append(f"=== {name} ===\n{code}")
    if not parts:
        raise ExtractionError("No source files were found in that .zip.")
    return "\n\n".join(parts)


def extract_text(path, code=False, name=None):
    """Extract text from a document, or (in code mode) source files, notebooks and .zip projects."""
    ext = extension(path)
    try:
        if ext == ".pdf":
            return _normalise(pdf_extract_text(path))
        if ext == ".docx":
            doc = Document(path)
            parts = [p.text for p in doc.paragraphs]
            for table in doc.tables:
                for row in table.rows:
                    parts.append(" | ".join(cell.text for cell in row.cells))
            return _normalise("\n".join(parts))
        if code and ext in ARCHIVE_EXTENSIONS:
            return _archive_text(path)
        if code and ext in CODE_EXTENSIONS:
            with open(path, "rb") as f:
                return f"=== {name or os.path.basename(path)} ===\n" + _code_text(path, f.read())
        if ext == ".txt":
            with open(path, encoding="utf-8", errors="replace") as f:
                return _normalise(f.read())
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"The file could not be read ({exc}).")
    raise ExtractionError("Unsupported file type.")
