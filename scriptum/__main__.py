"""`python -m scriptum` - serve the API and, if it is built, the client.

The mic modes record on the machine running this process, so run it on the
laptop that is in the room. `--host 0.0.0.0` then lets a phone or tablet on
the same network read the tabs while this machine listens.
"""
from __future__ import annotations

import argparse


def main() -> int:
    """Parse the server options and hand off to uvicorn."""
    p = argparse.ArgumentParser("scriptum", description=__doc__)
    p.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 to reach it from other devices on the network")
    p.add_argument("--port", type=int, default=8420)
    p.add_argument("--reload", action="store_true", help="reload on code changes")
    p.add_argument("--library", default=None,
                   help="folder holding the songs (default: the working directory)")
    args = p.parse_args()

    if args.library:
        import os
        os.environ["SCRIPTUM_LIBRARY"] = args.library

    import uvicorn
    print(f"  Scriptum → http://{args.host}:{args.port}")
    uvicorn.run("scriptum.app:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
