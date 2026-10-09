import os

DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///./app.db"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
CURRENCY = os.getenv("CURRENCY", "تومان")
CARD_NUMBER = os.getenv("CARD_NUMBER", "6037-0000-0000-0000")
CARD_HOLDER = os.getenv("CARD_HOLDER", "نام صاحب کارت")

AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a helpful, smart assistant. Always answer in the same language the user writes in.",
)
IMAGE_API_KEY = os.getenv("IMAGE_API_KEY") or AI_API_KEY
IMAGE_BASE_URL = (os.getenv("IMAGE_BASE_URL") or AI_BASE_URL).rstrip("/")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "")
VIDEO_MODEL = os.getenv("VIDEO_MODEL", "minimax/video-01")
