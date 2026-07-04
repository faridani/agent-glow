"""Allow `python -m hue_agent_status` (used on systems without the script shim)."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
