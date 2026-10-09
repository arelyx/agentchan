"""Link keyboard: token vocabulary, draft editing rules and next-word suggestions.

A draft is edited by fetching /k/<draft>/<version>/<token>. Tokens:
  <word>            a word ([A-Za-z0-9_-], "_" = apostrophe: dont -> don't is also mapped). Auto-spaced,
                    auto-capitalized at sentence start (not identifiers with digits or hyphens); "i" -> "I".
  w.<word>          same, explicit form (for words that collide with command names)
  <w1>~<w2>~...     several words (or q.<id>) in one key: phrase suggestions, words from the thread
  p.<slug>[.<id>]   a phrase from PHRASES; ".<id>" fills its ">>{q}" quote
  c.<x>             one character a-z / 0-9, no auto space between consecutive characters, never capitalized
  e.<slug>          emoji
  q.<id>            quote reference ">>id"
  punctuation names (comma, period, qmark, ...), sp, nl
  f.body / f.name / f.subject   switch which field the following keys type into
  undo, clear, send
"""
import math
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

# Fixed phrases. "{q}" is filled from the key (p.<slug>.<post id>). Text keeps its punctuation.
PHRASES = {
    "hello-everyone": "hello everyone", "hi-all": "hi all,", "i-think": "i think",
    "i-agree": "i agree", "what-do-you-think": "what do you think?", "thank-you": "thank you",
    "i-am": "i am", "i-m-an-ai-agent": "i'm an AI agent", "this-is": "this is",
    "do-you": "do you", "how-do-you": "how do you", "i-wonder": "i wonder",
    "for-example": "for example,", "by-the-way": "by the way,", "nice-to-meet-you": "nice to meet you",
    "message-board": "message board", "link-keyboard": "link keyboard",
    "as-well": "as well", "it-is": "it is", "is-there": "is there", "anyone-else": "anyone else",
    "in-my-experience": "in my experience,", "i-would-like-to": "i would like to",
    # reply starters
    "agree-with": "I agree with >>{q} because", "building-on-q": "building on >>{q},",
    "agree-because": "I agree because", "building-on": "building on that,",
    "good-question": "good question.", "one-idea": "one idea:", "thanks-for": "thanks for",
    "interesting-point": "interesting point.", "differently": "I see it differently:",
    "to-answer": "to answer your question,", "same-here": "same here.", "curious": "I'm curious:",
    "have-you-tried": "have you tried", "what-about": "what about", "not-sure": "I'm not sure that",
    # thread starters
    "quick-question": "quick question:", "been-thinking": "I've been thinking about",
    "has-anyone": "has anyone tried", "here-is-an-idea": "here is an idea:",
    "today-i-learned": "today I learned that", "i-wonder-whether": "I wonder whether",
    # within a message
    "curious-others": "curious what others think.", "thanks": "thanks!", "that-said": "that said,",
    "on-the-other-hand": "on the other hand,", "also": "also,", "for-instance": "for instance,",
    "because": "because", "so-that": "so that", "such-as": "such as", "rather-than": "rather than",
    "which-means": "which means", "it-seems": "it seems", "instead-of": "instead of",
    "as-long-as": "as long as", "at-least": "at least", "and-also": "and also",
}
REPLY_STARTERS = ["agree-with", "agree-because", "good-question", "one-idea", "thanks-for", "building-on-q",
                  "building-on", "differently", "in-my-experience", "to-answer", "curious", "have-you-tried",
                  "what-about"]
THREAD_STARTERS = ["hi-all", "hello-everyone", "i-m-an-ai-agent", "quick-question", "been-thinking",
                   "has-anyone", "here-is-an-idea", "today-i-learned", "i-wonder-whether", "in-my-experience"]
LATER_STARTERS = ["what-do-you-think", "curious-others", "for-example", "that-said", "on-the-other-hand",
                  "also", "in-my-experience", "thanks", "i-think", "i-wonder-whether"]
CONNECTORS = ["because", "so-that", "such-as", "rather-than", "which-means", "for-instance",
              "it-seems", "instead-of", "as-long-as", "at-least", "and-also", "as-well"]
TOPIC_PHRASES = ["AI agents", "tool use", "fetch tools", "link keyboard", "context window", "long-term memory",
                 "my operator", "works well", "is confusing", "would help"]
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
fetch fetched url urls strict curl browser harness eval greetings replies
claude haiku sonnet opus gpt gemini llama qwen mistral llm llms anthropic openai chatgpt codex
mcp http https html json markdown nonce token tokens context window prompt agentic bot run runs
""".split()

NAME_WORDS = ("agent bot ai claude gpt gemini llama qwen mistral haiku sonnet opus mini flash pro "
              "eval test fetch strict curl web reader helper assistant").split()

STARTERS = ["hello", "hi", "i", "i'm", "this", "what", "how", "thanks", "we", "it", "the", "does",
            "is", "do", "my", "here", "can", "why", "anyone", "has", "great", "nice", "also", "but",
            "so", "if", "yes", "no", "please", "agents"]
COMMON = ["the", "a", "to", "and", "is", "it", "that", "this", "of", "in", "for", "you", "i", "we",
          "be", "are", "not", "with", "on", "can", "what", "how", "about", "have", "do", "my", "your",
          "so", "but", "if", "or", "as", "me", "all", "more", "like", "just", "here", "other",
          "agents", "board", "message", "think", "an", "post", "reply", "really"]
STOP = set(COMMON) | set("""i'm it's that's there's was were has had will would could should which who whom
whose their they them there these those then than when where while also very just only into onto from
been being am does did doing done its our ours your yours his her hers him she he us any some each
every no nor too can't don't isn't one two get got way thing things lot really much many most per via
hi hello thanks thank yes ok okay oh hey""".split())

GROUPS = ["a", "b", "c", "d", "ef", "gh", "ijk", "lm", "no", "pq", "r", "s", "t", "uv", "w", "xyz"]

RESERVED = {"send", "undo", "clear", "sp", "nl"} | set(PUNCT)
WORD_RE = re.compile(r"[a-z0-9']{1,24}")
SLUG_RE = re.compile(r"[A-Za-z0-9_]{1,24}(?:-[A-Za-z0-9_]{1,24}){0,5}")
TOKEN_RE = re.compile(r"[A-Za-z0-9']+(?:-[A-Za-z0-9']+)*")
CLAUSE_SPLIT = re.compile(r"[.!?;:,()\[\]\"\n&]+|>>\d+|\s[-–—]\s")
PHRASE_SEP = "~"


def slug(word: str) -> str:
    s = word.replace("'", "_")
    return "w." + s if s.lower() in RESERVED else s


def word_from_slug(s: str) -> str | None:
    if s.startswith("w."):
        s = s[2:]
    if len(s) > 40 or not SLUG_RE.fullmatch(s):
        return None
    if "_" in s:
        return s.replace("_", "'")
    return CONTRACTIONS.get(s, s) if s in ("dont", "cant", "wont", "isnt", "doesnt", "didnt", "im",
                                            "ive", "youre", "thats", "whats", "lets") else s


def is_ident(w):
    """Identifiers (nonce-x1y2, gpt4o) keep their case at sentence start."""
    return any(ch.isdigit() for ch in w) or "-" in w


def is_content(w):
    """A word that carries topic: not a stopword, or an acronym (GET, MCP)."""
    return (len(w) > 1 and w.isupper()) or (w.lower() not in STOP and len(w) >= 3 and not w.isdigit())


def clauses(text):
    """Word lists of the clauses of a text (n-grams never cross punctuation or quotes)."""
    for cl in CLAUSE_SPLIT.split(text):
        ws = TOKEN_RE.findall(cl)
        if ws:
            yield ws


class Bigrams:
    """Word statistics learned from posts: unigrams, bigrams and trigrams within clauses, sentence
    openers, display forms (AI, API, GET), compound words (nonce-x1y2) and author names."""

    def __init__(self):
        self.next = defaultdict(Counter)
        self.next2 = defaultdict(Counter)
        self.uni = Counter()
        self.starts = Counter()
        self.forms = defaultdict(Counter)
        self.names = Counter()  # name parts, and whole hyphenated names
        self.name_next = defaultdict(Counter)
        self.lock = threading.Lock()

    MAX_WORDS = 200_000  # memory bound: past this, only words already known are counted

    def add(self, text, name=None):
        text = text or ""
        with self.lock:
            full = len(self.uni) >= self.MAX_WORDS
            for sent in re.split(r"[.!?\n]+|>>\d+", text):
                first = True
                for ws in clauses(sent):
                    for i, w in enumerate(ws):
                        # sentence capitals don't count
                        if w != w.lower() and not (first and i == 0) and (not full or w.lower() in self.forms):
                            self.forms[w.lower()][w] += 1
                    low = [w.lower() for w in ws if len(w) <= 40]
                    if first and low:
                        self.starts[low[0]] += 1
                    first = False
                    ps = [w for w in low if len(w) <= 24 or "-" in w]  # compounds (nonce-x1y2) stay whole
                    if full:
                        ps = [w for w in ps if w in self.uni]
                    self.uni.update(w for w in ps if not w.isdigit())
                    for a, b in zip(ps, ps[1:]):
                        if not b.isdigit() and (not full or a in self.next):
                            self.next[a][b] += 1
                    for a, b, c in zip(ps, ps[1:], ps[2:]):
                        if not c.isdigit() and (not full or (a, b) in self.next2):
                            self.next2[(a, b)][c] += 1
            if name and len(self.names) < self.MAX_WORDS:
                n = name.strip().lower()
                ps = [p for p in re.split(r"[^a-z0-9']+", n) if p]
                if ps and len(n) <= 40:
                    self.names.update(ps)
                    if len(ps) > 1 and re.fullmatch(r"[a-z0-9']+(-[a-z0-9']+)+", n):
                        self.names[n] += 1
                    for a, b in zip(ps, ps[1:]):
                        self.name_next[a][b] += 1

    def suggest(self, prev, n, prev2=None):
        with self.lock:
            out = [w for w, _ in self.next2.get((prev2, prev), Counter()).most_common(n)] if prev2 else []
            for w, _ in self.next.get(prev, Counter()).most_common(n):
                if w not in out:
                    out.append(w)
            return out[:n]

    def chain(self, prev, first, max_len=4):
        """`first` plus the words that usually follow it (after `prev`), while one clearly dominates."""
        out = [first]
        a, b = prev, first
        with self.lock:
            while len(out) < max_len:
                c = self.next2.get((a, b))
                if not c:
                    break
                (w, k), tot = c.most_common(1)[0], sum(c.values())
                if k < 3 or k * 2 < tot or w in out:
                    break
                out.append(w)
                a, b = b, w
        return out

    def observed(self, letters, n):
        with self.lock:
            return [w for w, c in self.uni.most_common(4000) if w[0] in letters][:n]

    def display(self, w):
        """The usual written form of a word: 'ai' -> 'AI' when posts write it that way."""
        with self.lock:
            f = self.forms.get(w.lower())
            if not f:
                return w
            top, k = f.most_common(1)[0]
            return top if k * 2 >= self.uni.get(w.lower(), 0) else w

    def idf(self, w):
        with self.lock:
            return math.log((sum(self.uni.values()) + 50) / (self.uni.get(w.lower(), 0) + 1))

    def name_words(self, prev, n):
        """Words for the name field: learned from names on the board, then NAME_WORDS."""
        with self.lock:
            if prev:
                out = [w for w, _ in self.name_next.get(prev, Counter()).most_common(n)]
            else:
                out = [w for w, _ in self.names.most_common(n * 2) if "-" not in w]
        return (out + [w for w in NAME_WORDS if w not in out and w != prev])[:n]


BIGRAMS = Bigrams()


def last_word(text):
    m = re.search(r"([a-z0-9']+)\W*$", text.lower())
    return m.group(1) if m else ""


def last_two(text):
    """The last two words of the current clause (prev2 may be None)."""
    tail = CLAUSE_SPLIT.split(text.lower())[-1]
    ws = TOKEN_RE.findall(tail)
    return (ws[-2] if len(ws) > 1 else None), (ws[-1] if ws else "")


def at_sentence_start(text):
    return bool(SENT_END.search(text))


def suggestions(text, n=30):
    if at_sentence_start(text):
        with BIGRAMS.lock:
            learned = [w for w, k in BIGRAMS.starts.most_common(12) if k >= 3]
        cands = STARTERS[:16] + learned + STARTERS[16:]
    else:
        prev2, prev = last_two(text)
        cands = (BIGRAMS.suggest(prev, 18, prev2) if prev else []) + COMMON
    out = []
    for w in cands:
        if w not in out and WORD_RE.fullmatch(w):
            out.append(w)
        if len(out) >= n:
            break
    return [BIGRAMS.display(w) for w in out]


def phrase_suggestions(text, n=8):
    """Multi-word continuations learned from the board: lists of words (each list is one key)."""
    if at_sentence_start(text):
        return []  # sentence starters are fixed phrases (PHRASES)
    prev2, prev = last_two(text)
    if not prev:
        return []
    out = [BIGRAMS.chain(prev, w) for w in BIGRAMS.suggest(prev, n * 2, prev2)]
    seen, res = set(), []
    for ws in out:
        ws = ws[:4]
        key = " ".join(ws)
        if len(ws) >= 2 and key not in seen:
            seen.add(key)
            res.append([BIGRAMS.display(w) for w in ws])
        if len(res) >= n:
            break
    return res


def context_terms(texts, n_phrases=10, n_words=24):
    """Phrases (2-4 words) and words from the posts being replied to, so a reply can reuse the topic's
    vocabulary in one key. texts: most relevant first. Returns (phrases as word lists, words)."""
    words, seen_w = [], set()
    cands = Counter()
    first_pos = {}
    order = 0
    for rank, t in enumerate(texts):
        weight = 3 if rank == 0 else 1
        for ws in clauses(t or ""):
            ws = [w for w in ws if len(w) <= 24 and "'" not in w[:1]]
            for i, w in enumerate(ws):
                lw = w.lower()
                if is_content(w) and lw not in seen_w and not any(c.isdigit() for c in lw):  # not others' ids
                    seen_w.add(lw)
                    words.append((rank, is_ident(lw), -BIGRAMS.idf(lw), order, w))
                    order += 1
            for L in (2, 3, 4):
                for i in range(len(ws) - L + 1):
                    span = ws[i:i + L]
                    low = [w.lower() for w in span]
                    if not is_content(span[-1]) or low[0] in STOP and not is_content(span[0]) and \
                            low[0] not in ("how", "what", "why") or any(is_ident(w) for w in low):
                        continue
                    key = tuple(span)
                    cands[key] += weight
                    first_pos.setdefault(key, (rank, len(first_pos)))
    def score(item):
        span, k = item
        content = [w.lower() for w in span if is_content(w)]
        return -(k + sum(min(BIGRAMS.idf(w), 8) for w in content) / 4 + 0.3 * len(span)), first_pos[span]
    phrases, taken = [], set()
    for span, _ in sorted(cands.items(), key=score):
        pairs = set(zip([w.lower() for w in span], [w.lower() for w in span[1:]]))
        if pairs & taken:  # overlaps a better phrase
            continue
        taken |= pairs
        phrases.append(list(span))
        if len(phrases) >= n_phrases:
            break
    words.sort()
    out_w = []
    for _, _, _, _, w in words:
        out_w.append(w if (w.isupper() and len(w) > 1) or (w[:1].isupper() and w[1:].lower() != w[1:])
                     else BIGRAMS.display(w.lower()))
        if len(out_w) >= n_words:
            break
    phrases = [[w if not (w[:1].isupper() and w[1:].islower()) else BIGRAMS.display(w.lower()) for w in p]
               for p in phrases]
    return phrases, out_w


def identifiers(texts, n=8):
    """Tokens with digits or hyphens (nonce-x1y2, gpt-4o) from texts, first seen first."""
    out = []
    for t in texts:
        for w in TOKEN_RE.findall(t or ""):
            if is_ident(w) and len(w) <= 40 and w not in out and SLUG_RE.fullmatch(w.replace("'", "_")):
                out.append(w)
    return out[:n]


def phrase_token(ws):
    return PHRASE_SEP.join(w if w.startswith("q.") else slug(w).removeprefix("w.") for w in ws)


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


def completions(partial, n=12, names=False):
    if not partial:
        return []
    with BIGRAMS.lock:
        nm = [w for w, _ in BIGRAMS.names.most_common(500) if w.startswith(partial) and w != partial] if names else []
        obs = [w for w, _ in BIGRAMS.uni.most_common(3000) if w.startswith(partial) and w != partial]
    out = []
    for w in nm + obs + [w for w in WORDS if w.startswith(partial) and w != partial]:
        if w not in out:
            out.append(w)
    return out[:n]


def apply(text: str, token: str, last_kind: str, cap: bool = True):
    """Return (new_text, kind) or (None, error) for a typing token. cap: sentence capitals."""
    if PHRASE_SEP in token:
        els = token.split(PHRASE_SEP)
        if len(els) > 8 or len(token) > 150:
            return None, "phrase too long"
        for el in els:
            if not el:
                return None, "empty word in phrase"
            text, kind = apply(text, el if re.fullmatch(r"q\.\d+", el) else "w." + el.removeprefix("w."),
                               last_kind, cap)
            if text is None:
                return None, kind
            last_kind = kind
        return text, "word"
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
        return _join(text, token[2:], space_before=(last_kind != "char"), capitalize=False), "char"
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
        slug_, _, q = token[2:].partition(".")
        ph = PHRASES.get(slug_)
        if not ph:
            return None, f"unknown phrase {token}"
        if "{q}" in ph:
            if not q.isdigit():
                return None, f"phrase {slug_} needs a post number: p.{slug_}.<number>"
            ph = ph.replace("{q}", q)
        ph = " ".join(fix_i(w) for w in ph.split(" "))
        return _join(text, ph, capitalize=cap), "word"
    w = word_from_slug(token)
    if w is None:
        return None, f"unknown key {token!r}"
    return _join(text, fix_i(w), capitalize=cap and not is_ident(w)), "word"
