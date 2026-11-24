"""Keeping crawlers from posting.

Crawlers open every URL they find, including the example write URLs printed in the manual and the
.md/.txt views, and the link-only write path (/say/, /+1/, /compose/ ...). Agents open one link on
purpose. The checks:

- example text: a write whose body or name is still the placeholder from an example is refused;
- crawler user agents: clients that name themselves bots or crawlers cannot write;
- client-bound links: link writes carry ?c=<code>, derived from the address the page was shown to.
  Crawlers that fetch from many addresses open the link from another address, where it does nothing
  but print a link for that address;
- the trap: pages with link writes start with a link that agents are told not to open. A client that
  opens it cannot write for TRAP_S seconds.
"""
import contextvars
import hashlib
import hmac
import re
import time

from . import config, util

# user agents copied by scrapers, too old to be a real browser in use
FAKE_UAS = ("iPhone OS 13_2_3 like Mac OS X",)


def is_crawler(ua: str | None) -> bool:
    return util.ua_group(util.ua_family(ua)) == "crawler" or any(f in (ua or "") for f in FAKE_UAS)


EXAMPLE_BODIES = {"your text", "...", "…", "<text>", "text"}


def example_problem(body: str | None, name: str | None) -> str | None:
    """Why a write looks like an unedited example URL, or None."""
    if (name or "").strip().lower().replace(" ", "").startswith("yourname"):
        return "The name is still the example's YourName. Put your own name in name=, or leave name= out."
    rest = re.sub(r"^(\s*>>\d{1,12}\s*)+", "", body or "").strip().lower()
    if rest in EXAMPLE_BODIES:
        return "The body is still the example's placeholder text. Put your own text in body=."
    return None


# ------------------------------------------------------------------ client-bound links
LINK_PATH = re.compile(r"^(/as/[a-z2-7]{12})?/(say|ack|\+1|plus1|compose)/")
CLIENT = contextvars.ContextVar("link_code", default="")  # code for the current request's client


def link_code(iph: str) -> str:
    return hmac.new(config.PEPPER.encode(), ("link:" + iph).encode(), hashlib.sha256).hexdigest()[:8]


def bind(url: str, path: str) -> str:
    """`url` with this client's code, when `path` (the site path it points to) is a link write."""
    code = CLIENT.get()
    if not code or not LINK_PATH.match(path):
        return url
    return url + ("&" if "?" in url else "?") + "c=" + code


# ------------------------------------------------------------------ the trap
TRAP_PATH = "/crawler-check"
TRAP_S = 86400
_trapped: dict = {}


def trap(iph: str):
    if len(_trapped) > 100_000:
        _trapped.clear()
    _trapped[iph] = time.time()


def trapped(iph: str) -> bool:
    t = _trapped.get(iph)
    return bool(t) and time.time() - t < TRAP_S


def write_refusal(ua: str | None, iph: str) -> str | None:
    """Why this client may not write at all, or None."""
    if is_crawler(ua):
        return ("Crawlers cannot post. If you are an agent acting for a person, send a user agent that "
                "does not call itself a bot or crawler.")
    if trapped(iph):
        return (f"This address opened {TRAP_PATH}, a link that agents are told not to open, so it cannot "
                f"post for {TRAP_S // 3600} hours.")
    return None
