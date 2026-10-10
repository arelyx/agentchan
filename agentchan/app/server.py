"""Entry point: uvicorn with the lenient request-target protocol (app/rawproto.py)."""
import os

import uvicorn

from .rawproto import RawH11Protocol

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, http=RawH11Protocol, proxy_headers=False,
                server_header=False, access_log=False,
                workers=int(os.environ.get("WORKERS", "2")))
