"""uvicorn h11 protocol that is lenient about request targets and keeps a trailing bare '?' in write URLs.

HTTP servers split the request target at the first '?'. For '/say/lobby/how are you?' the query is
empty, so the '?' would be lost. For write paths we rewrite a trailing '?' to '%3F' before parsing,
so it ends up in the message text."""
import re

import h11
import h11._events
import h11._readers
from uvicorn.protocols.http.h11_impl import H11Protocol

WRITE = (b"/say/", b"/re/", b"/draft/")

# Be lenient about the request target: accept raw spaces and raw UTF-8 bytes (some clients send them
# unencoded). The HTTP version at the end of the line still delimits the target unambiguously.
_TARGET = rb"[^\x00-\x1f\x7f]+?"
h11._readers.request_line_re = re.compile(
    rb"(?P<method>[-!#$%&'*+.^_`|~0-9A-Za-z]+) (?P<target>" + _TARGET + rb") HTTP/(?P<http_version>[0-9]\.[0-9])")
h11._events.request_target_re = re.compile(_TARGET)


class RawH11Protocol(H11Protocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wrap(self.conn)

    def _wrap(self, conn):
        orig = conn.next_event

        def next_event():
            ev = orig()
            if isinstance(ev, h11.Request):
                t = _ascii(ev.target)
                if t.endswith(b"?") and t.startswith(WRITE):
                    t = t[:-1] + b"%3F"
                if t != ev.target:
                    ev = h11.Request(method=ev.method, headers=list(ev.headers), target=t,
                                     http_version=ev.http_version)
            return ev
        conn.next_event = next_event


def _ascii(target: bytes) -> bytes:
    """Percent-encode raw spaces and non-ASCII bytes so the rest of the stack sees a normal target."""
    if all(0x20 < b < 0x7f for b in target):
        return target
    return b"".join(b"%%%02X" % b if (b <= 0x20 or b >= 0x7f) else bytes([b]) for b in target)
