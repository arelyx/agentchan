import uvicorn

from . import config
from .rawproto import RawH11Protocol

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=config.PORT, http=RawH11Protocol,
                proxy_headers=False, server_header=False, log_level="info")
