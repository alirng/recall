"""Scrub secrets before anything is written to the index.

The index is read back into other agents' context windows, so a key pasted into
one chat must not resurface in another. Patterns err on the side of redacting, but
ordinary hashes, UUIDs and commit ids stay: only a credential-shaped token, or a long token
that sits next to a word like "api key", "secret" or "password", is masked.

Every rule is named, so a scan can say which one fired. VERSION is a hash of the rules (see
the end of RULES): when a rule changes it changes, and chats/scrub.py then rewrites the text
recall keeps and cannot rebuild from the agents' files (candidates, llm_cache, the archive).
"""

import hashlib
import re
from collections import Counter

MARK = "[REDACTED]"

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


def _assignment(m: re.Match) -> str:
    return f"{m.group(1)}{m.group(2)}{MARK}"


def _tail_unless_path(m: re.Match) -> str:
    return m.group(0) if _is_path_or_host(m.group("v")) else _tail(m)


_COOKIE_CH = r"[A-Za-z0-9_.~%+/\-]"
# name=value inside a Cookie header; the value has a letter and a digit and is 12+ characters long (a theme or a
# flag is not). The name is anchored so a long run without "=" is read once.
_COOKIE_PAIR = re.compile(
    rf"(?<![A-Za-z0-9_.\-])(?P<n>[A-Za-z0-9_.\-]+=)(?!\[?REDACTED)"
    rf"(?P<c>(?={_COOKIE_CH}*\d)(?={_COOKIE_CH}*[A-Za-z]){_COOKIE_CH}{{12,}}={{0,2}})"
)


def _cookie(m: re.Match) -> str:
    return m.group(0)[: m.start("v") - m.start()] + _COOKIE_PAIR.sub(lambda p: p.group("n") + MARK, m.group("v"))


def _cookie_values(m: re.Match) -> list[str]:
    """What _cookie replaces, for redact_many: the cookie values, not the rest of the header line."""
    return [p.group("c") for p in _COOKIE_PAIR.finditer(m.group("v"))]


_cookie.values = _cookie_values  # type: ignore[attr-defined]


_VENDOR_PREFIXES = (
    r"gl(?:pat|dt|rt|ptt)-[A-Za-z0-9_\-]{20,}"  # GitLab: personal, deploy, runner, pipeline trigger
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
    r"|pypi-[A-Za-z0-9_\-]{50,}"  # PyPI: a macaroon, 100+ characters
    r"|dckr_pat_[A-Za-z0-9_\-]{20,}"  # Docker Hub
    r"|GOCSPX-[A-Za-z0-9_\-]{20,}"  # Google OAuth client secret
)

_PEM_HEAD = r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
_PEM_END = r"-----END [A-Z ]*PRIVATE KEY-----"
_NL = r"(?:\s|\\r|\\n)"  # a line break, also the \n a JSON string holds
_EOL = r"(?:\r?\n|\\r?\\n)"
_PEM_OPEN = (
    # Proc-Type: / DEK-Info: lines of an encrypted key; a header line ends at its line break, so a line of
    # "a: a: a:" has one way to be read
    rf"(?:[ \t]*{_EOL})?(?:[ \t]*[A-Za-z][A-Za-z\-]*:[^\r\n\\]*{_EOL})*"
    rf"{_NL}*(?=[A-Za-z0-9+/]*\d)[A-Za-z0-9+/]{{24,}}={{0,2}}(?:{_NL}+[A-Za-z0-9+/]{{16,}}={{0,2}})*"
)

# (name, pattern, replacement). The shapes come first, each a credential wherever it appears.
RULES: list[tuple[str, re.Pattern, object]] = [
    # a whole block; or, when the END line is missing (a cut-off paste, a message cut at 20,000 characters),
    # the BEGIN line and the base64 lines after it, if there are any (prose after a mention of the header stays)
    ("private-key", re.compile(rf"{_PEM_HEAD}.*?{_PEM_END}|{_PEM_HEAD}{_PEM_OPEN}", re.S), MARK),
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
    # the path of a Slack or Discord webhook is the secret: whoever has the URL can post
    (
        "webhook-url",
        re.compile(
            r"(?i)\bhttps?://(?:hooks\.slack\.com/(?:services|workflows|triggers|actions)"
            r"|(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks)/(?P<v>[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*)"
        ),
        _tail_unless_placeholder,
    ),
    ("db-url", re.compile(r"(?i)\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:/@]*:[^\s@]+@"), MARK),
    # any other scheme://user:password@host, keeping everything but the password
    (
        "url-userinfo",
        re.compile(r"(?i)\b[a-z][a-z0-9+.\-]{0,31}://[^\s:/@\[\]]*:(?P<v>(?!\[REDACTED\])[^\s@/\[\]]{6,})@"),
        _tail_unless_placeholder,
    ),
    # scheme://token@host: a user part that is itself a credential (git clone https://<token>@host/...),
    # not ssh://git@host or postgres://postgres@localhost, and not abfss://<workspace-guid>@host: in a
    # storage URL (abfss, s3, gs ...) the part before the @ names a container, and a GUID is an id
    (
        "url-token-user",
        re.compile(
            r"(?i)\b(?!(?:abfss?|wasbs?|adls?|s3a?|gs|hdfs|oss|cos|obs)://)[a-z][a-z0-9+.\-]{0,31}://"
            r"(?P<v>(?!\[?REDACTED)(?![0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}@)"
            r"(?=[A-Za-z0-9_\-.~%]*\d)(?=[A-Za-z0-9_\-.~%]*[A-Za-z])[A-Za-z0-9_\-.~%]{20,})@"
        ),
        _tail_unless_placeholder,
    ),
    # Authorization: Bearer <token>
    (
        "bearer",
        re.compile(rf"(?i)\bbearer\s+(?P<v>{_tok(20)})"),
        _tail_unless_path,
    ),
    # Authorization: Basic <base64 of user:password>. Base64 has no "-" or "_", and a mix of upper and lower case
    # with a digit; "basic authentication-flow-v2" and "basic ConfigurationManager" are words.
    (
        "basic",
        re.compile(
            r"(?i)\bbasic\s+(?P<v>(?!\[?REDACTED)(?-i:(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*[A-Z]))"
            r"(?=[A-Za-z0-9+/]*\d)[A-Za-z0-9+/]{16,}={0,2})(?![A-Za-z0-9+/=\-_.])"
        ),
        _tail,
    ),
    # name=value, name: value. The original rule, which the rules below widen.
    (
        "assignment",
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:api[_-]?key|secret|token|password|passwd|pwd|client[_-]?secret|access[_-]?key)[A-Z0-9_]*)"
            r"(\s*[:=]\s*[\"']?)([^\s\"']{8,})"
        ),
        _assignment,
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
    # SERVICE_API_KEY=..., MY_APP_ENCRYPTION_KEY=...: an upper-case name that ends in _KEY.
    # SSH_KEY=~/.ssh/... is a path, not a value.
    (
        "env-key",
        re.compile(
            rf"\b(?![A-Z0-9_]*(?:CACHE|SORT|PRIMARY|FOREIGN|PARTITION|STORAGE|ROUTE|QUERY))[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_KEY(?![A-Za-z0-9])[\s\"'`*]{{0,3}}[:=]\s*[\"'`]?(?P<v>{_NOT_MARK}{_tok(16)})"
        ),
        _tail_unless_path,
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
    # Cookie: session=<value>; Set-Cookie: ...: every cookie value that looks like a credential, the names stay
    (
        "cookie",
        re.compile(r"(?i)\b(?:set-)?cookie[\"']?[ \t]*:[ \t]*[\"']?(?P<v>[^\r\n]+)"),
        _cookie,
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
    "vendor-prefix": (
        *("glpat-", "gldt-", "glrt-", "glptt-", "dop_v1_", "hf_", "r8_", "pplx-", "gsk_", "xai-", "tvly-", "sg."),
        *("npm_", "shp", "pypi-", "dckr_pat_", "gocspx-"),
    ),
    "webhook-url": ("hooks.slack.com", "/webhooks/"),
    "db-url": ("://",),
    "url-userinfo": ("://",),
    "url-token-user": ("://",),
    "bearer": ("bearer",),
    "basic": ("basic",),
    "assignment": ("api", "secret", "token", "pass", "pwd", "access"),
    "named-value": _KEYWORDS,
    "named-password": ("pass", "pwd"),
    "prose-key": _KEYWORDS,
    "env-key": ("_key",),
    "bare-key": ("key",),
    "prose-bare-key": ("key",),
    "url-param": (*_KEYWORDS, "auth", "sig", "sas", "x-amz", "code_verifier"),
    "cookie": ("cookie",),
}
assert {name for name, *_ in RULES} == set(_NEEDS)

# Bump when the logic around the patterns changes without a pattern changing (redact_many, _is_path_or_host, a helper).
_LOGIC = "1"


def _version() -> str:
    """A hash of the rules: the pattern, flags and replacement of each, the words that switch them on, the placeholders.
    It changes exactly when what redact() does to some text can change, and then chats/scrub.py runs again."""
    h = hashlib.sha256(_LOGIC.encode())
    for name, pattern, repl in RULES:
        h.update(
            repr((name, pattern.pattern, pattern.flags, repl if isinstance(repl, str) else repl.__name__)).encode()
        )
    h.update(repr(sorted(_NEEDS.items())).encode())
    h.update(_PLACEHOLDER.pattern.encode())
    h.update(_COOKIE_PAIR.pattern.encode())
    return h.hexdigest()[:10]


VERSION = _version()


def _rules(text: str):
    low = text.lower()
    return [(n, p, r) for n, p, r in RULES if any(w in low for w in _NEEDS[n])]


def redact(text: str) -> str:
    return _apply(text, None)


def _apply(text: str, found: dict[str, None] | None) -> str:
    """Run the rules. found collects the credential values they replace (in order), for redact_many."""
    if not text:
        return text
    for _, pattern, repl in _rules(text):
        text = pattern.sub(repl if found is None else _collecting(repl, found), text)
    return text


def _collecting(repl, found: dict[str, None]):
    def sub(m: re.Match) -> str:
        new = repl(m) if callable(repl) else repl
        if new != m.group(0):
            if hasattr(repl, "values"):  # a rule whose group v is more than the secret says which parts are
                values = repl.values(m)
            else:
                values = [m.group("v") if "v" in m.re.groupindex and m.group("v") else m.group(0)]
            for v in values:
                if _is_token(v):
                    found[v] = None
                for r in _LONG_RUN.findall(v):  # the part that turns up alone
                    if _is_token(r):
                        found[r] = None
        return new

    return sub


_LONG_RUN = re.compile(r"[A-Za-z0-9]{20,}")
_CORE = re.compile(r"[A-Za-z0-9]{16,}")
_RUN = re.compile(rf"{_TOKCH}{{16,}}")


def _is_token(v: str) -> bool:
    return len(v) >= 16 and re.search(r"\d", v) is not None and re.search(r"[A-Za-z]", v) is not None


# How many of a chat's values are looked for as plain substrings in every text. The cost is values x text,
# so a chat that holds hundreds (an .env dump, a fixture file) hands the rest to a token lookup, which
# reads each text once.
EXACT_VALUES = 32


def redact_many(texts: list[str]) -> list[str]:
    """redact() over the texts of one chat. A key pasted after "api key:" is often pasted again on its
    own further down (in a command, an .env listing), where no word marks it: every value the rules
    replaced in any of the texts is replaced in all of them (the first EXACT_VALUES wherever they sit, the
    rest where they stand as a token of their own, or as the 20+ character alphanumeric core of one)."""
    found: dict[str, None] = {}
    out = [_apply(t, found) for t in texts]
    if found:
        values = list(found)
        exact = sorted(values[:EXACT_VALUES], key=len, reverse=True)
        rest = values[EXACT_VALUES:]
        out = [_repeats(t, exact, rest) if t else t for t in out]
    return out


def _repeats(text: str, exact: list[str], rest: list[str]) -> str:
    for v in exact:
        if v in text:
            text = text.replace(v, MARK)
    if rest:
        text = _by_token(text, frozenset(rest), frozenset(c for v in rest for c in _CORE.findall(v)))
    return text


def _by_token(text: str, whole: frozenset[str], cores: frozenset[str]) -> str:
    def run(m: re.Match) -> str:
        t = m.group(0)
        if t in whole:
            return MARK
        return _CORE.sub(lambda c: MARK if c.group(0) in cores else c.group(0), t)

    return _RUN.sub(run, text)


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
