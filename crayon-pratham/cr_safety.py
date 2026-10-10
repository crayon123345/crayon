"""Secret redaction. Used on everything that is logged, stored or sent."""
import os
import re

_PATTERNS = [
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),            # telegram bot token
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),                  # google api key
    re.compile(r"\bpostgres(?:ql)?://[^\s]+", re.I),            # db urls
    re.compile(r"\bnpg_[A-Za-z0-9]{10,}\b"),                    # neon password
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}\b"),         # generic api keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),              # github tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_ -]?key|token)\b\s*[:=]\s*\S{6,}"),
]
_ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "DATABASE_URL", "CRAYON_ADMIN_TOKEN", "GOOGLE_CLIENT_SECRET", "GOOGLE_WORKSPACE_CLIENT_SECRET", "GITHUB_OAUTH_CLIENT_SECRET", "CRAYON_VAULT_MASTER_KEY", "GOOGLE_TOKEN_ENCRYPTION_KEY", "CRAYON_BRIDGE_TOKEN", "OPENROUTER_API_KEY", "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN", "TELEGRAM_OIDC_CLIENT_SECRET", "CRAYON_WEB_ENCRYPTION_KEY")


def _env_secrets():
    vals = []
    for k in _ENV_KEYS:
        v = os.environ.get(k, "")
        if len(v) >= 8:
            vals.append(v)
    return vals


def redact(text):
    if not text:
        return text
    out = str(text)
    for v in _env_secrets():
        out = out.replace(v, "[redacted]")
    for p in _PATTERNS:
        out = p.sub("[redacted]", out)
    return out


def looks_like_secret(text):
    """True when a user message appears to contain a credential."""
    if not text:
        return False
    return any(p.search(text) for p in _PATTERNS)


def clean_text(text):
    """Telegram-safe plain text. Preserve languages, URLs and ordinary punctuation."""
    out = redact(text) or ""
    out = out.translate(str.maketrans({"\u2014":" - ", "\u2013":"-", "\u2018":"'", "\u2019":"'", "\u201c":'"', "\u201d":'"', "\u2022":"-", "\u25cf":"-", "\u25aa":"-", "\u2026":"..."}))
    out = re.sub(r"\[([^]\n]+)\]\((https?://[^\s)]+)\)", r"\1: \2", out)
    out = re.sub(r"```[^\n]*\n?", "", out)
    out = re.sub(r"\*\*(.*?)\*\*", r"\1", out, flags=re.S)
    out = re.sub(r"__(.*?)__", r"\1", out, flags=re.S)
    out = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", out)
    out = re.sub(r"(?m)^\s*\*\s+", "- ", out)
    out = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", out)
    out = re.sub(r"`([^`\n]+)`", r"\1", out)
    return out.strip()
