"""Crayon runtime config. Everything comes from environment variables."""
import os


def env(key, default=""):
    return os.environ.get(key, default)


TELEGRAM_TOKEN = env("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_FALLBACKS = [m.strip() for m in env("GEMINI_FALLBACK_MODELS", "gemini-3.1-flash-lite,gemini-3-flash-preview").split(",") if m.strip()]
# LLM layer: LangChain on top, Gemini default. LLM_PROVIDER=gemini|openrouter, LLM_FRAMEWORK=langchain|direct
LLM_PROVIDER = env("LLM_PROVIDER", "gemini").strip().lower()
LLM_FRAMEWORK = env("LLM_FRAMEWORK", "langchain").strip().lower()
OPENROUTER_KEY = env("OPENROUTER_API_KEY")
OPENROUTER_MODEL = env("OPENROUTER_MODEL", "openrouter/free")
OPENROUTER_FALLBACKS = [m.strip() for m in env("OPENROUTER_FALLBACK_MODELS", "").split(",") if m.strip()]
DATABASE_URL = env("DATABASE_URL")
ADMIN_TOKEN = env("CRAYON_ADMIN_TOKEN")  # guards /selftest, /tick
DEFAULT_TZ = env("CRAYON_TZ", "Asia/Kolkata")
MODE = env("CRAYON_MODE", "polling")  # polling | webhook
PUBLIC_URL = env("CRAYON_PUBLIC_URL", "https://crayon-v1.onrender.com")
HISTORY_TURNS = int(env("CRAYON_HISTORY_TURNS", "14"))
DAILY_MESSAGE_CAP = int(env("CRAYON_DAILY_CAP", "80"))
# WhatsApp channel (all optional; read via env() in cr_whatsapp.py): WHATSAPP_ACCESS_TOKEN,
# WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN, WHATSAPP_GRAPH_VERSION,
# CRAYON_OWNER_WA_ID (or WHATSAPP_ALLOWED_IDS, comma list)
VERSION = "2.35.0"

OPENROUTER_AUTO_FALLBACK = env("OPENROUTER_AUTO_FALLBACK", "off").lower() == "on"
