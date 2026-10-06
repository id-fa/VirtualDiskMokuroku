"""ソースツリーから直接 GUI を起動するためのランチャー(ダブルクリックで起動できる)。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from virtualdiskmokuroku.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
