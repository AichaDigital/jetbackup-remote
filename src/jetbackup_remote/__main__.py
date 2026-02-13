"""Allow running as: python -m jetbackup_remote."""

import sys

from .cli import main

sys.exit(main())
