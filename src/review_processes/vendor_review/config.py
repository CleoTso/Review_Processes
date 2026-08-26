from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True, slots=True)
class Config:
    airtable_token: str
    airtable_base_id: str
    airtable_vendor_table: str
    airtable_vendor_view: str
    google_token_file: Path
    state_dir: Path
    lookback_days: int
    history_days: int

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        missing = []
        if not (os.getenv("AIRTABLE_API_KEY") or os.getenv("AIRTABLE_TOKEN")):
            missing.append("AIRTABLE_API_KEY")
        if missing:
            raise SystemExit(f"Missing environment variables: {', '.join(missing)}")
        return cls(
            airtable_token=os.getenv("AIRTABLE_API_KEY") or os.environ["AIRTABLE_TOKEN"],
            airtable_base_id=os.getenv("AIRTABLE_BASE_ID") or "app25k6lMy8bzOhq5",
            airtable_vendor_table=os.getenv("AIRTABLE_VENDOR_TABLE") or "tblmysPS8GSncnWSa",
            airtable_vendor_view=os.getenv("AIRTABLE_VENDOR_VIEW") or "viwVD8IFpH6fXUPvh",
            google_token_file=Path(os.getenv("GOOGLE_TOKEN_FILE", "google-token.json")),
            state_dir=Path(os.getenv("REVIEW_STATE_DIR", ".review-state")),
            lookback_days=int(os.getenv("REVIEW_LOOKBACK_DAYS", "180")),
            history_days=int(os.getenv("REVIEW_HISTORY_DAYS", "730")),
        )

