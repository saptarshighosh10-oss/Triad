# Load .env BEFORE importing cli (which imports config). config reads TRIAD_* / *_API_KEY
# at import time, so the .env values must already be in os.environ or those overrides — model
# IDs, TRIAD_CLAUDE_BASE_URL, etc. — are silently ignored. Real env vars still win (no override).
from pathlib import Path

from . import dotenv

dotenv.load(Path(__file__).resolve().parent.parent / ".env")

from .cli import main  # noqa: E402  (must follow the .env load above)

if __name__ == "__main__":
    main()
