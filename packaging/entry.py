"""PyInstaller entry point for cratekeeper.app.

Double-clicked from Finder (no arguments) it opens the desktop window.
Run from a terminal with a subcommand, the same binary is the full CLI:

    cratekeeper.app/Contents/MacOS/cratekeeper scan
    cratekeeper.app/Contents/MacOS/cratekeeper install-service
"""

import os
import sys

# Windowed PyInstaller apps can start without std streams; libraries such as
# uvicorn's log formatter probe them, so give them something harmless.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w"))


def _main() -> int:
    # Finder on old macOS versions passed a -psn_... process serial number.
    args = [a for a in sys.argv[1:] if not a.startswith("-psn")]
    if args:
        from cratekeeper.cli import main as cli_main

        return cli_main(args)
    from cratekeeper.app import main as app_main

    return app_main()


if __name__ == "__main__":
    sys.exit(_main())
