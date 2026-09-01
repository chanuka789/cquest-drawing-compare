"""Run this to confirm the development environment is ready."""
import importlib
import platform
import shutil
import sys

REQUIRED = [
    "fastapi", "uvicorn", "webview", "pydantic", "sqlalchemy",
    "pypdfium2", "pikepdf", "cv2", "numpy", "PIL", "scipy",
    "skimage", "shapely", "ezdxf", "rapidfuzz", "pandas",
    "openpyxl", "docx", "xxhash", "loguru", "typer", "httpx",
    "anthropic",
]

OPTIONAL = ["sentence_transformers", "torch", "pytesseract"]


def check(name):
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


print("=" * 56)
print("  C-Quest Drawing Compare — environment check")
print("=" * 56)
print(f"Python   : {sys.version.split()[0]}")
print(f"Windows  : {platform.platform()}")
print(f"Venv     : {'YES' if sys.prefix != sys.base_prefix else 'NO  <-- ACTIVATE IT'}")
print(f"Node     : {'found' if shutil.which('node') else 'MISSING'}")
print(f"Git      : {'found' if shutil.which('git') else 'MISSING'}")
print("-" * 56)

missing = [p for p in REQUIRED if not check(p)]
for p in REQUIRED:
    print(f"  {'ok ' if check(p) else 'MISSING'}  {p}")

print("-" * 56)
print("Optional (needed from Phase 5 / 8):")
for p in OPTIONAL:
    print(f"  {'ok ' if check(p) else '-  '}  {p}")

print("=" * 56)
if missing:
    print(f"RESULT: {len(missing)} package(s) missing: {', '.join(missing)}")
    print("Fix with: pip install -r requirements.txt")
else:
    print("RESULT: Ready. You can start Phase 1.")
print("=" * 56)