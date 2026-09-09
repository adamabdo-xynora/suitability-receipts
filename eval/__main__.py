"""`uv run python -m eval`: run the set, print the report, exit non-zero on failure."""

import sys

from eval.scorer import main

sys.exit(main())
