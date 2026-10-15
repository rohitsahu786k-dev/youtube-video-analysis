"""Build a deployable Python source ZIP without credentials or runtime data."""
from pathlib import Path
import hashlib
import json
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist" / "trader-video-intelligence.zip"
FILES = ["run.py", "requirements.txt", "README.md", "HOW_IT_WORKS_HINGLISH.md",
         "Dockerfile", "docker-compose.yml", ".dockerignore", ".env.example", "requirements-dev.txt", "tools/build.py", "tools/qa_browser.py"]
SECRET = re.compile(rb"AIza[0-9A-Za-z_-]{30,}|gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{30,}|sk-(?:proj-)?[0-9A-Za-z_-]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


def build():
    paths = [ROOT / name for name in FILES]
    paths.extend(p for directory in ("pyapp", "docs", "deploy") for p in (ROOT / directory).rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    for path in paths:
        if SECRET.search(path.read_bytes()):
            raise RuntimeError(f"Possible credential in {path.relative_to(ROOT)}; build stopped.")
    OUTPUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            archive.write(path, path.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(OUTPUT) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Archive integrity check failed")
        assert "pyapp/templates/charts.html" in archive.namelist()
        assert "pyapp/templates/backtesting.html" in archive.namelist()
        assert "pyapp/engine/simulator.py" in archive.namelist() and "deploy/Caddyfile" in archive.namelist()
        assert ".env" not in archive.namelist()
        assert not any(name.startswith("storage/") for name in archive.namelist())
    metadata = {"artifact": OUTPUT.name, "files": len(paths), "bytes": OUTPUT.stat().st_size,
                "sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest()}
    (OUTPUT.parent / "build.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata))


if __name__ == "__main__":
    build()
