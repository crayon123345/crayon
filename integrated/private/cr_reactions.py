"""Deterministic content-aware Telegram reactions; no model quota."""
import re

def choose(text):
    t = (text or "").strip().lower()
    if not t or t.startswith("/") or re.fullmatch(r"(yes|y|no|n|confirm|confirmed|cancel|stop|sure|go ahead|do it|[a-d]|[0-9]+)[.!]?", t):
        return None
    if re.search(r"\b(thanks|thank you|love you|ty|thankyou)\b", t):
        return "❤️"
    if re.search(r"\b(congrats|congratulations|birthday|celebrate|celebration|passed|graduated)\b", t):
        return "🎉"
    if re.search(r"\b(yoo+|yo+|let.?s goo*|yay+|won|nailed|awesome|amazing|excited)\b", t):
        return "🔥"
    if re.search(r"\b(lol|lmao|haha+|joke|funny)\b", t):
        return "😂"
    if re.search(r"\b(build|make|find|research|check|remind|schedule|draft|update|fix|do)\b", t):
        return "👀"
    if "?" in t or re.match(r"(what|why|how|when|where|who|which|can|could|is|does|are)\b", t):
        return "🤔"
    if re.search(r"\b(ok|okay|got it|continue|nice|good|done|alright)\b", t):
        return "👍"
    return "👍"
