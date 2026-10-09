"""uvicorn h11 protocol that accepts raw spaces and raw UTF-8 bytes in the request target
(ported from variants/stream).

Some clients send `GET /reply?thread=1&body=hi there, how are you? HTTP/1.1` without encoding.
h11 rejects such request lines. Here the request-line regex is relaxed (the trailing " HTTP/x.y"
still delimits the target unambiguously) and raw spaces / non-ASCII bytes are percent-encoded
before the rest of the stack sees the target. A '?' inside the body text needs no help: only the
first '?' separates path and query.
"""
import re

import h11
import h11._events
import h11._readers
from uvicorn.protocols.http.h11_impl import H11Protocol

_TARGET = rb"[^\x00-\x1f\x7f]+?"
h11._readers.request_line_re = re.compile(
    rb"(?P<method>[-!#$%&'*+.^_`|~0-9A-Za-z]+) (?P<target>" + _TARGET + rb") HTTP/(?P<http_version>[0-9]\.[0-9])")
h11._events.request_target_re = re.compile(_TARGET)


def _ascii(target: bytes) -> bytes:
    if all(0x20 < b < 0x7f for b in target):
        return target
    return b"".join(b"%%%02X" % b if (b <= 0x20 or b >= 0x7f) else bytes([b]) for b in target)


class RawH11Protocol(H11Protocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        orig = self.conn.next_event

        def next_event():
            ev = orig()
            if isinstance(ev, h11.Request):
                t = _ascii(ev.target)
                if t != ev.target:
                    ev = h11.Request(method=ev.method, headers=list(ev.headers), target=t,
                                     http_version=ev.http_version)
            return ev
        self.conn.next_event = next_event
