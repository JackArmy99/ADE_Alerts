"""`python -m ade_radar ...` (same as the ade-radar command; avoids the generated .exe launcher)."""

import sys

from .cli import main

sys.exit(main())
