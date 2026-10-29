"""Replace REPLACE_* placeholders in a dev .env with fresh random secrets. Never touches set values."""

import base64
import re
import secrets
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else ".env")
text = path.read_text()
fills = {
    "DATA_KEYS": lambda: "k1:" + base64.b64encode(secrets.token_bytes(32)).decode(),
    "SESSION_PEPPER": lambda: secrets.token_urlsafe(32),
    "MOCK_AGE_SIGNING_SECRET": lambda: secrets.token_urlsafe(32),
}
changed = []
for key, gen in fills.items():
    pattern = re.compile(rf"^{key}=.*REPLACE.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(f"{key}={gen()}", text)
        changed.append(key)
path.write_text(text)
print("generated:", ", ".join(changed) if changed else "nothing (already set)")
