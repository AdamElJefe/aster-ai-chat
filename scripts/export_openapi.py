"""Write the OpenAPI document generated from the FastAPI app (the contract source of truth)."""

import base64
import json
import sys
from pathlib import Path

from aster_api.core.config import Settings
from aster_api.main import create_app

settings = Settings(
    app_env="dev",
    data_keys="k1:" + base64.b64encode(b"\0" * 32).decode(),
    session_pepper="export",
    mock_age_signing_secret="export",
)
spec = create_app(settings, manage_logging=False).openapi()
out = Path(sys.argv[1])
out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
print(f"wrote {out} ({len(spec['paths'])} paths)")
