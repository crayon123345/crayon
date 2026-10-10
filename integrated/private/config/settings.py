from dataclasses import dataclass
import os

@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "gemma4:e4b")
    openrouter_models: tuple[str, ...] = tuple(x.strip() for x in os.getenv("OPENROUTER_MODELS", "").split(",") if x.strip())
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_service_role_key: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    token_encryption_key: str = os.getenv("TOKEN_ENCRYPTION_KEY", "")
    google_client_secret_file: str = os.getenv("GOOGLE_CLIENT_SECRET_FILE", "config/google_client_secret.json")
    oauth_redirect_uri: str = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8080/oauth/callback")
    daily_summary_enabled: bool = os.getenv("DAILY_SUMMARY_ENABLED", "false").lower() == "true"

    def validate_for_bot(self) -> list[str]:
        missing = []
        if not self.telegram_bot_token: missing.append("TELEGRAM_BOT_TOKEN")
        if not (self.ollama_base_url or self.openrouter_api_key or self.gemini_api_key): missing.append("one model backend")
        return missing
