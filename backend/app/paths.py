"""Where FireLens keeps its side files (seasonal archive, map-context tiles, per-fire reports).

Default: `data/` at the repository root, as in development. In a container set FIRELENS_DATA_DIR
(e.g. /data) so they live on a persistent volume next to the database.
"""

import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("FIRELENS_DATA_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "data"
