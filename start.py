"""Compatibility entrypoint for hosts configured to run start.py.

The production Voroa worker lives in voroa_launcher.py.  Some deployment
configs may still invoke `python start.py`; keeping this tiny shim prevents
that stale command from breaking the service.
"""
from voroa_launcher import run


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("[Voroa] Shutdown requested.", flush=True)
