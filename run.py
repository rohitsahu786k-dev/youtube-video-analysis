import os
import sys
import uvicorn
from pathlib import Path

if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    # Ensure UTF-8 output on Windows consoles
    os.environ["PYTHONIOENCODING"] = "utf-8"
    print("=" * 60)
    print("  Forex Trader Video Intelligence (Python + Gemini AI)")
    print("  Web App URL: http://localhost:8000")
    print("  API Docs:    http://localhost:8000/docs")
    print("=" * 60)
    uvicorn.run("pyapp.main:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")), reload=False)
