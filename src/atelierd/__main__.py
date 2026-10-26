"""`python -m atelierd`: the daemon's command line, which is how the systemd unit and the window start it."""

import sys

from atelierd.daemon.cli import main

sys.exit(main())
