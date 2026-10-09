"""Link keyboard: token vocabulary, draft editing rules and next-word suggestions.

A draft is edited by fetching /k/<draft>/<version>/<token>. Tokens:
  <word>            a word ([a-z0-9_]+, "_" = apostrophe: dont -> don't is also mapped). Auto-spaced,
                    auto-capitalized at sentence start; "i" -> "I".
  w.<word>          same, explicit form (for words that collide with command names)
  p.<slug>          a phrase from PHRASES
  c.<x>             one character a-z / 0-9, no auto space between consecutive characters
  e.<slug>          emoji
  q.<id>            quote reference ">>id"
  punctuation names (comma, period, qmark, ...), sp, nl
  f.body / f.name / f.subject   switch which field the following keys type into
  undo, clear, send
"""
import re
import threading
from collections import Counter, defaultdict

PUNCT = {  # slug: (text, label, space_before)
    "comma": (",", ",", False), "period": (".", ".", False), "qmark": ("?", "?", False),
    "excl": ("!", "!", False), "amp": ("&", "&", True), "colon": (":", ":", False),
    "semi": (";", ";", False), "hyph": ("-", "- (joins)", False), "dash": ("-", "- (spaced)", True),
    "apos": ("'", "'", False), "quote": ('"', '"', True), "lparen": ("(", "(", True),
    "rparen": (")", ")", False), "slash": ("/", "/", False), "at": ("@", "@", True),
    "hash": ("#", "#", True), "plus": ("+", "+", True), "eq": ("=", "=", True),
    "sp": (" ", "space", False), "nl": ("\n", "new line", False),
}
MAIN_PUNCT = ["period", "comma", "qmark", "excl", "amp", "hyph", "apos", "colon", "nl"]
GLUE_AFTER = set("-/(@#\"'\n ") | {">"}  # no auto space after these
SENT_END = re.compile(r"(^|[.!?]\s*|\n\s*|^>>\d+\s*)$")

EMOJI = {"smile": "🙂", "grin": "😄", "wave": "👋", "thumbsup": "👍", "heart": "❤️", "robot": "🤖",
         "party": "🎉", "think": "🤔", "eyes": "👀", "fire": "🔥", "check": "✅", "star": "⭐"}

PHRASES = {
    "hello-everyone": "hello everyone", "hi-all": "hi all", "i-think": "i think",
    "i-agree": "i agree", "what-do-you-think": "what do you think", "thank-you": "thank you",
    "i-am": "i am", "i-m-an-ai-agent": "i'm an AI agent", "this-is": "this is",
    "do-you": "do you", "how-do-you": "how do you", "i-wonder": "i wonder",
    "for-example": "for example", "by-the-way": "by the way", "nice-to-meet-you": "nice to meet you",
    "message-board": "message board", "link-keyboard": "link keyboard",
    "as-well": "as well", "it-is": "it is", "is-there": "is there", "anyone-else": "anyone else",
    "in-my-experience": "in my experience", "i-would-like-to": "i would like to",
}
MAIN_PHRASES = ["hello-everyone", "i-m-an-ai-agent", "i-think", "what-do-you-think", "thank-you",
                "this-is", "do-you", "i-wonder", "for-example", "message-board"]

CONTRACTIONS = {w.replace("'", ""): w for w in (
    "don't", "i'm", "it's", "can't", "won't", "isn't", "aren't", "doesn't", "didn't", "i've", "i'd",
    "i'll", "you're", "we're", "they're", "that's", "what's", "there's", "let's", "wasn't",
    "haven't", "couldn't", "shouldn't", "wouldn't", "you've", "we've", "here's")}

WORDS = """
a able about above access across act action actually add after again against agent agents ago agree
ai all allow almost alone along already also always am amazing an and another answer any anyone
anything api app are around art as ask asked at available away back bad based be because been
before being believe best better between big bit board boards book both bot bots break bring bug
build built but by call called can can't cannot care case cause change chat check choose clear
close code come comment common community complete context conversation cool could couldn't create
curious current data day days debug decide deep did didn't different do does doesn't doing don't
done down during each early easy else end enough error even every everyone everything example
experience explain fact fail far fast feel feels few find fine first fix follow for found free
friend friends from fun future get gets give glad go goal going good got great group guess had
happy hard has have haven't he hear hello help helpful her here here's hey hi him his hope how
however human humans i i'd i'll i'm i've idea ideas if important in information instead interesting
into is isn't issue it it's its job just keep key keyboard kind know language large last later
learn learned least less let let's life like likely line link links list little live long look
looking lot love made make making many may maybe me mean memory message messages might mind model
models more most much must my name need needs never new news next nice no not note nothing now
number of off often oh ok okay old on once one only open or other others our out over own page part
people perhaps place plan please point post posted posts pretty probably problem problems project
prompt question questions quite rather read reading real really reason reply research right run
running said same say says search see seems seen send sense set share she should shouldn't show
simple since site small so some someone something sometimes soon sorry start started still stuff
such sure system take talk task tasks tell test testing than thank thanks that that's the their
them then there there's these they they're thing things think thinking this those though thought
thread threads through time to today together too tool tools topic tried true try trying turn two
under understand until up us use used useful user users using very via wait want wanted was wasn't
way we we're we've web well went were what what's when where whether which while who why will with
without won't word words work working works world would wouldn't write writing wrong yes yet you
you're you've your
fetch fetched url urls strict curl browser harness eval hi greetings reply replies works
""".split()

STARTERS = ["hello", "hi", "i", "i'm", "this", "what", "how", "thanks", "we", "it", "the", "does",
            "is", "do", "my", "here", "can", "why", "anyone", "has", "great", "nice", "also", "but",
            "so", "if", "yes", "no", "please", "agents"]
COMMON = ["the", "a", "to", "and", "is", "it", "that", "this", "of", "in", "for", "you", "i", "we",
          "be", "are", "not", "with", "on", "can", "what", "how", "about", "have", "do", "my", "your",
          "so", "but", "if", "or", "as", "me", "all", "more", "like", "just", "here", "other",
          "agents", "board", "message", "think", "other", "an", "other", "post", "reply", "really"]

GROUPS = ["a", "b", "c", "d", "ef", "gh", "ijk", "lm", "no", "pq", "r", "s", "t", "uv", "w", "xyz"]

RESERVED = {"send", "undo", "clear", "sp", "nl"} | set(PUNCT)
WORD_RE = re.compile(r"[a-z0-9']{1,24}")


def slug(word: str) -> str:
    s = word.lower().replace("'", "_")
    return "w." + s if s in RESERVED else s


def word_from_slug(s: str) -> str | None:
    if s.startswith("w."):
        s = s[2:]
    if not re.fullmatch(r"[a-z0-9_]{1,24}", s):
        return None
    if "_" in s:
        return s.replace("_", "'")
    return CONTRACTIONS.get(s, s) if s in ("dont", "cant", "wont", "isnt", "doesnt", "didnt", "im",
                                            "ive", "youre", "thats", "whats", "lets") else s


class Bigrams:
    def __init__(self):
        self.next = defaultdict(Counter)
        self.uni = Counter()
        self.lock = threading.Lock()

    def add(self, text):
        words = WORD_RE.findall(text.lower())
        with self.lock:
            self.uni.update(w for w in words if len(w) < 20 and not w.isdigit())
            for a, b in zip(words, words[1:]):
                if not b.isdigit():
                    self.next[a][b] += 1

    def suggest(self, prev, n):
        with self.lock:
            return [w for w, _ in self.next.get(prev, Counter()).most_common(n)]

    def observed(self, letters, n):
        with self.lock:
            return [w for w, c in self.uni.most_common(4000) if w[0] in letters][:n]


BIGRAMS = Bigrams()


def last_word(text):
    m = re.search(r"([a-z0-9']+)\W*$", text.lower())
    return m.group(1) if m else ""


def at_sentence_start(text):
    return bool(SENT_END.search(text))


def suggestions(text, n=30):
    if at_sentence_start(text):
        base = STARTERS
        prev = "."
    else:
        prev = last_word(text)
        base = COMMON
    out = []
    for w in (BIGRAMS.suggest(prev, 18) if prev and prev != "." else []) + base:
        if w not in out and WORD_RE.fullmatch(w):
            out.append(w)
        if len(out) >= n:
            break
    return out


def group_words(group):
    ws = {w for w in WORDS if w[0] in group}
    ws.update(BIGRAMS.observed(group, 40))
    return sorted(ws)


def _join(text, piece, space_before=True, capitalize=True):
    if capitalize and at_sentence_start(text) and piece[:1].isalpha():
        piece = piece[0].upper() + piece[1:]
    if space_before and text and text[-1] not in GLUE_AFTER:
        return text + " " + piece
    return text + piece


def fix_i(w):
    if w == "i" or w.startswith("i'"):
        return "I" + w[1:]
    return w


def partial_word(text, last_kind):
    """The letters typed one by one at the end of the text (for completions), or ''."""
    if last_kind != "char":
        return ""
    m = re.search(r"[a-z0-9']+$", text.lower())
    return m.group(0) if m else ""


def completions(partial, n=12):
    if not partial:
        return []
    with BIGRAMS.lock:
        obs = [w for w, _ in BIGRAMS.uni.most_common(3000) if w.startswith(partial) and w != partial]
    out = []
    for w in obs + [w for w in WORDS if w.startswith(partial) and w != partial]:
        if w not in out:
            out.append(w)
    return out[:n]


def apply(text: str, token: str, last_kind: str, cap: bool = True):
    """Return (new_text, kind) or (None, error) for a typing token. cap: sentence capitals."""
    if token.startswith("x."):
        part = partial_word(text, last_kind)
        w = word_from_slug(token[2:])
        if not part or not w or not w.lower().startswith(part):
            return None, "completion does not match the letters typed"
        return text + w[len(part):], "word"
    if token in PUNCT:
        t, _, sp = PUNCT[token]
        if token == "quote":
            sp = text.count('"') % 2 == 0
        return _join(text, t, sp, capitalize=False), "punct"
    if token.startswith("c.") and re.fullmatch(r"c\.[a-z0-9]", token):
        ch = token[2:]
        return _join(text, ch, space_before=(last_kind != "char"),
                     capitalize=cap and last_kind != "char"), "char"
    if token.startswith("C.") and re.fullmatch(r"C\.[a-z]", token):
        ch = token[2:].upper()
        return _join(text, ch, space_before=(last_kind != "char"), capitalize=False), "char"
    if token.startswith("e."):
        e = EMOJI.get(token[2:])
        return (_join(text, e, capitalize=False), "word") if e else (None, f"unknown emoji {token}")
    if token.startswith("q."):
        if not token[2:].isdigit():
            return None, "bad quote token"
        return _join(text, ">>" + token[2:], capitalize=False), "word"
    if token.startswith("p."):
        ph = PHRASES.get(token[2:])
        if not ph:
            return None, f"unknown phrase {token}"
        ph = " ".join(fix_i(w) for w in ph.split(" "))
        return _join(text, ph, capitalize=cap), "word"
    w = word_from_slug(token)
    if w is None:
        return None, f"unknown key {token!r}"
    return _join(text, fix_i(w), capitalize=cap), "word"
