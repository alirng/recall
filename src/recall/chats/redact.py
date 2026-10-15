"""Scrub secrets before anything is written to the index.

The index is read back into other agents' context windows, so a key pasted into
one chat must not resurface in another. Patterns err on the side of redacting, but
ordinary hashes, UUIDs and commit ids stay: only a credential-shaped token, or a long token
that sits next to a word like "api key", "secret" or "password", is masked.

Every rule is named, so a scan can say which one fired. When a rule changes, bump
VERSION: chats/scrub.py then rewrites the text recall keeps and cannot rebuild from the
agents' files (candidates, llm_cache, the archive).
"""

import re
from collections import Counter

MARK = "[REDACTED]"
VERSION = "2"

_TOKCH = r"[A-Za-z0-9_\-+/=.~%\\]"  # what a pasted key is made of, with the \_ a markdown escape adds
_NOT_MARK = r"(?![\[]?REDACTED)"


def _tok(n: int) -> str:
    """A value that looks like a credential: n or more characters with a letter and a digit in them."""
    return rf"(?={_TOKCH}*\d)(?={_TOKCH}*[A-Za-z]){_TOKCH}{{{n},}}"


# The word starts a name ("api_key", "ClientSecret", "x-api-key"), it is not inside a longer word.
_LEFT = r"(?:(?<![A-Za-z0-9])|(?-i:(?<=[a-z0-9])(?=[A-Z])))"

# Words that name a credential. "key" alone is left out: foreign key, React key, sort key.
_PASSWORD = r"passw(?:or)?d|passwd|pwd|passphrase"
_NAMES = (
    r"api[ _\-]?(?:key|token|secret)|apikey"
    r"|(?:client|app|consumer|signing|encryption|webhook|shared|account|master|private|secret|access|license"
    r"|subscription|auth|session|refresh|id|bearer)[ _\-]?(?:key|secret|token)"
    r"|secret|token|credentials?|fedauth"
)
# The words that count in prose, where nothing but a space separates the word from the value.
_PROSE_WORDS = (
    r"api[ _\-]?(?:key|token|secret)|apikey"
    r"|(?:client|signing|access|license|subscription|private|secret)[ _\-]?(?:key|secret|token)"
    r"|secret|token|" + _PASSWORD
)
# What may follow the word in a name: secret_key, API_KEY_PROD. Not tokens, secretName, private_key_path.
_SUFFIX = r"(?:[_\-](?:key|value|prod|production|dev|test|staging|local|\d{1,3}))*"
# What may sit between a name and its value: closing quotes, markdown, a separator.
_SEP = r"""[\s"'`*\]\)]{0,4}(?:[:=]|%3[AaDd]|=>|->)[\s*]*["'`(<]{0,3}"""


def _is_path_or_host(v: str) -> bool:
    return (
        v.startswith(("/", "./", "../", "~/"))
        or re.fullmatch(r"[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+){2,}\.?", v) is not None
        or v.count("/") >= 3
    )


_PLACEHOLDER = re.compile(
    r"(?i)(?:\$|\{|<|%\{|\*+$|x{3,}$|your|changeme|example|placeholder|password$|passwd$|pass$|secret$|token$)"
)


def _tail_unless_placeholder(m: re.Match) -> str:
    return m.group(0) if _PLACEHOLDER.match(m.group("v")) or _is_path_or_host(m.group("v")) else _tail(m)


def _tail(m: re.Match) -> str:
    """Replace only the value (group v), keeping the name and separator in front of it."""
    return m.group(0)[: m.start("v") - m.start()] + MARK


def _tail_unless_path(m: re.Match) -> str:
    return m.group(0) if _is_path_or_host(m.group("v")) else _tail(m)


_VENDOR_PREFIXES = (
    r"glpat-[A-Za-z0-9_\-]{20,}"  # GitLab
    r"|dop_v1_[0-9a-f]{40,}"  # DigitalOcean
    r"|hf_[A-Za-z0-9]{30,}"  # Hugging Face
    r"|r8_[A-Za-z0-9]{30,}"  # Replicate
    r"|pplx-[A-Za-z0-9]{30,}"  # Perplexity
    r"|gsk_[A-Za-z0-9]{40,}"  # Groq
    r"|xai-[A-Za-z0-9]{40,}"  # xAI
    r"|tvly-[A-Za-z0-9\-]{20,}"  # Tavily
    r"|SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"  # SendGrid
    r"|npm_[A-Za-z0-9]{36}"  # npm
    r"|shp(?:at|ss|ca|pa)_[0-9a-f]{32}"  # Shopify
    r"|ntn_[A-Za-z0-9]{40,}"  # Notion
    r"|lin_api_[A-Za-z0-9]{36,}"  # Linear
    r"|figd_[A-Za-z0-9_\-]{30,}"  # Figma
    r"|sbp_[0-9a-f]{40}"  # Supabase
    r"|whsec_[A-Za-z0-9]{20,}"  # Stripe webhook
    r"|ya29\.[A-Za-z0-9_\-]{20,}"  # Google OAuth
    r"|key-[0-9a-f]{32}"  # Mailgun
)

# (name, pattern, replacement). The shapes come first, each a credential wherever it appears.
RULES: list[tuple[str, re.Pattern, object]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), MARK),
    ("sk-key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"), MARK),
    ("github", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"), MARK),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"), MARK),
    ("aws", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), MARK),
    ("slack", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), MARK),
    ("google-aiza", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), MARK),
    ("stripe", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{20,}"), MARK),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), MARK),
    # sk_ then 36+ characters with a digit in them: ElevenLabs (48 hex), Cartesia (sk_car_...)
    ("sk_-key", re.compile(r"\b(?:sk|rk)_(?=[A-Za-z0-9_]*\d)[A-Za-z0-9_]{36,}"), MARK),
    ("firecrawl", re.compile(r"(?<![A-Za-z0-9])fc-[0-9a-f]{32}\b"), MARK),
    # Azure AD client secret: three characters, a digit, "Q~", then 31-34 more
    ("azure-ad-secret", re.compile(r"(?<![A-Za-z0-9_.~\-])[A-Za-z0-9_.\-]{3}\dQ~[A-Za-z0-9_.~\-]{30,40}"), MARK),
    ("vendor-prefix", re.compile(rf"(?<![A-Za-z0-9_\-])(?:{_VENDOR_PREFIXES})"), MARK),
    ("db-url", re.compile(r"(?i)\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:/@]+:[^\s@]+@"), MARK),
    # any other scheme://user:password@host, keeping everything but the password
    (
        "url-userinfo",
        re.compile(r"(?i)\b[a-z][a-z0-9+.\-]{0,31}://[^\s:/@\[\]]+:(?P<v>(?!\[REDACTED\])[^\s@/\[\]]{6,})@"),
        _tail_unless_placeholder,
    ),
    # Authorization: Bearer <token>, Basic <base64>
    (
        "bearer",
        re.compile(rf"(?i)\b(?:bearer|basic)\s+(?P<v>{_tok(20)})"),
        _tail_unless_path,
    ),
    # name=value, name: value. The original rule, which the rules below widen.
    (
        "assignment",
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:api[_-]?key|secret|token|password|passwd|pwd|client[_-]?secret|access[_-]?key)[A-Z0-9_]*)"
            r"(\s*[:=]\s*[\"']?)([^\s\"']{8,})"
        ),
        lambda m: f"{m.group(1)}{m.group(2)}{MARK}",
    ),
    # "apiKey": "...", **Client Secret**: `...`, API Key: ..., api_key%3D...: a name, then a separator
    (
        "named-value",
        re.compile(rf"(?i){_LEFT}(?:{_NAMES}|{_PASSWORD}){_SUFFIX}{_SEP}(?P<v>{_NOT_MARK}{_tok(12)})"),
        _tail,
    ),
    # the same for a password, which need not look like a key
    (
        "named-password",
        re.compile(
            rf"(?i){_LEFT}(?:{_PASSWORD}){_SUFFIX}{_SEP}(?P<v>{_NOT_MARK}(?=[^\s\"'`<>]*[\d!@#$%^&*+=~?])[^\s\"'`<>]{{8,}})"
        ),
        _tail_unless_placeholder,
    ),
    # use this api key <value>, the api key is <value>
    (
        "prose-key",
        re.compile(
            rf"(?i){_LEFT}(?:{_PROSE_WORDS})(?:\s+(?:is|are|was|=|:|->)\s+|\s+)[\"'`]?(?P<v>{_NOT_MARK}{_tok(20)})"
        ),
        _tail_unless_path,
    ),
    # INDEXNOW_KEY=..., WEBSITE_AUTH_ENCRYPTION_KEY=...: an upper-case name that ends in _KEY
    (
        "env-key",
        re.compile(
            rf"\b(?![A-Z0-9_]*(?:CACHE|SORT|PRIMARY|FOREIGN|PARTITION|STORAGE|ROUTE|QUERY))[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_KEY(?![A-Za-z0-9])[\s\"'`*]{{0,3}}[:=]\s*[\"'`]?(?P<v>{_NOT_MARK}{_tok(16)})"
        ),
        _tail,
    ),
    # a bare "Key: <long mixed token>" at 32+ characters (not a hash: it needs upper and lower case)
    (
        "bare-key",
        re.compile(
            rf"(?i)(?<![A-Za-z0-9])key(?![A-Za-z0-9])[\s\"'`*]{{0,3}}[:=]\s*[\"'`]?"
            rf"(?P<v>{_NOT_MARK}(?-i:(?={_TOKCH}*[a-z])(?={_TOKCH}*[A-Z])(?={_TOKCH}*\d)){_TOKCH}{{32,}})"
        ),
        _tail_unless_path,
    ),
    # "this key <value>", "my key is <value>": a mixed-case token of 24+, which a hash or a UUID never is
    (
        "prose-bare-key",
        re.compile(
            rf"(?i)(?<![A-Za-z0-9])(?:this|my|your|our|the)\s+key(?![A-Za-z0-9])(?:\s+is|\s*[:=])?\s+[\"'`]?"
            rf"(?P<v>{_NOT_MARK}(?-i:(?={_TOKCH}*[a-z])(?={_TOKCH}*[A-Z])(?={_TOKCH}*\d)){_TOKCH}{{24,}})"
        ),
        _tail_unless_path,
    ),
    # ?apiKey=..., &access_token=..., %3Fkey%3D...: a query parameter that carries a credential
    (
        "url-param",
        re.compile(
            r"(?i)(?:[?&;]|%3F|%26|&amp;|\\u0026)"
            r"(?:api[_\-]?key|apikey|key|token|access[_\-]?token|refresh[_\-]?token|id[_\-]?token|auth[_\-]?token|auth"
            r"|secret|client[_\-]?secret|password|pwd|sig|signature|sas|x-api-key|subscription-key|ocp-apim-subscription-key"
            r"|x-amz-(?:signature|security-token|credential)|code[_\-]?verifier)"
            r"(?:=|%3D|\\u003d)(?P<v>(?!\[?REDACTED)(?=[^&\s\"'`<>)\]\\]*\d)(?=[^&\s\"'`<>)\]\\]*[A-Za-z])[^&\s\"'`<>)\]\\]{8,})"
        ),
        _tail,
    ),
]


# A rule only runs on text that contains one of these (lower case), which is most of the speed:
# nearly every message is plain prose, and the patterns are the slow part.
_KEYWORDS = ("key", "secret", "token", "pass", "pwd", "credential", "fedauth")
_NEEDS: dict[str, tuple[str, ...]] = {
    "private-key": ("private key",),
    "sk-key": ("sk-",),
    "github": ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),
    "github-pat": ("github_pat_",),
    "aws": ("akia", "asia"),
    "slack": ("xox",),
    "google-aiza": ("aiza",),
    "stripe": ("sk_", "rk_", "pk_"),
    "jwt": ("eyj",),
    "sk_-key": ("sk_", "rk_"),
    "firecrawl": ("fc-",),
    "azure-ad-secret": ("q~",),
    "vendor-prefix": ("glpat-", "dop_v1_", "hf_", "r8_", "pplx-", "gsk_", "xai-", "tvly-", "sg.", "npm_", "shp"),
    "db-url": ("://",),
    "url-userinfo": ("://",),
    "bearer": ("bearer", "basic"),
    "assignment": ("api", "secret", "token", "pass", "pwd", "access"),
    "named-value": _KEYWORDS,
    "named-password": ("pass", "pwd"),
    "prose-key": _KEYWORDS,
    "env-key": ("_key",),
    "bare-key": ("key",),
    "prose-bare-key": ("key",),
    "url-param": (*_KEYWORDS, "auth", "sig", "sas", "x-amz", "code_verifier"),
}
assert {name for name, *_ in RULES} == set(_NEEDS)


def _rules(text: str):
    low = text.lower()
    return [(n, p, r) for n, p, r in RULES if any(w in low for w in _NEEDS[n])]


def redact(text: str) -> str:
    return _apply(text, None)


def _apply(text: str, found: set[str] | None) -> str:
    """Run the rules. found collects the credential values they replace, for redact_many."""
    if not text:
        return text
    for _, pattern, repl in _rules(text):
        text = pattern.sub(repl if found is None else _collecting(repl, found), text)
    return text


def _collecting(repl, found: set[str]):
    def sub(m: re.Match) -> str:
        new = repl(m) if callable(repl) else repl
        if new != m.group(0):
            v = m.group("v") if "v" in m.re.groupindex and m.group("v") else m.group(0)
            if _is_token(v):
                found.add(v)
            found.update(r for r in re.findall(r"[A-Za-z0-9]{20,}", v) if _is_token(r))  # the part that turns up alone
        return new

    return sub


def _is_token(v: str) -> bool:
    return len(v) >= 16 and re.search(r"\d", v) is not None and re.search(r"[A-Za-z]", v) is not None


def redact_many(texts: list[str]) -> list[str]:
    """redact() over the texts of one chat. A key pasted after "api key:" is often pasted again on its
    own further down (in a command, an .env listing), where no word marks it: every value the rules
    replaced in any of the texts is replaced in all of them."""
    found: set[str] = set()
    out = [_apply(t, found) for t in texts]
    if found:
        again = re.compile("|".join(re.escape(v) for v in sorted(found, key=len, reverse=True)))
        out = [again.sub(MARK, t) if t else t for t in out]
    return out


def redact_json(value):
    """redact() over every string in decoded JSON, so an escape in an encoded blob is never cut."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_json(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_json(v) for k, v in value.items()}
    return value


def count(text: str) -> Counter:
    """How many spans each rule changes in text, for scans and the pull request's numbers."""
    found: Counter = Counter()
    for name, pattern, repl in _rules(text):
        for m in pattern.finditer(text):
            if (repl(m) if callable(repl) else repl) != m.group(0):
                found[name] += 1
        text = pattern.sub(repl, text)
    return found
