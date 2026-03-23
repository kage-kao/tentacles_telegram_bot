"""
Shared config for Universal Telegram Bot.
All modules import bot, dp, and shared state from here.
"""
import os
import re
import random
import logging
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import BaseFilter
from aiogram.fsm.storage.memory import MemoryStorage

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
executor = ThreadPoolExecutor(max_workers=3)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("bot")

# ── Mode constants ──
MODE_AI = 1
MODE_VIDEO = 2
MODE_FILE = 3

MODE_NAMES = {
    MODE_AI: "AI Ассистент",
    MODE_VIDEO: "Видео инструменты",
    MODE_FILE: "Перезалив файлов",
}

# ── Shared state ──
user_mode: dict[int, int] = {}
user_settings: dict[int, dict] = {}
global_keys_pool: list[str] = []
ADMINS: set[int] = set()


class ModeFilter(BaseFilter):
    """Фильтр: пропускает только если пользователь в нужном режиме."""
    def __init__(self, mode: int):
        self.mode = mode

    async def __call__(self, event, *args, **kwargs) -> bool:
        user = None
        if hasattr(event, 'from_user'):
            user = event.from_user
        elif hasattr(event, 'message') and hasattr(event.message, 'from_user'):
            user = event.message.from_user
        if user is None:
            return False
        return user_mode.get(user.id, MODE_AI) == self.mode

# ── Chat models ──
CHAT_PROVIDERS = {
    "openai": ["gpt-4o", "gpt-4o-mini"],
    "anthropic": ["claude-sonnet-4-20250514", "claude-haiku-4-20250414"],
    "gemini": ["gemini-2.5-flash", "gemini-2.0-flash"],
}
CHAT_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "gemini": "Google Gemini",
}
CHAT_MODEL_LABELS = {
    "gpt-4o": "GPT-4o",
    "gpt-4o-mini": "GPT-4o Mini",
    "claude-sonnet-4-20250514": "Claude Sonnet 4",
    "claude-haiku-4-20250414": "Claude Haiku 4",
    "gemini-2.5-flash": "Gemini 2.5 Flash",
    "gemini-2.0-flash": "Gemini 2.0 Flash",
}

# ── Video gen constants ──
VIDEO_MODELS = ["sora-2", "sora-2-pro"]
VIDEO_SIZES = ["1280x720", "1792x1024", "1024x1792", "1024x1024"]
VIDEO_SIZE_LABELS = {
    "1280x720": "1280x720 HD",
    "1792x1024": "1792x1024 Wide",
    "1024x1792": "1024x1792 Portrait",
    "1024x1024": "1024x1024 Square",
}
VIDEO_DURATIONS = [4, 8, 12]

IMAGE_QUALITY = ["low", "medium", "high"]
TTS_MODELS = ["tts-1", "tts-1-hd"]
TTS_VOICES = ["alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"]

MAX_RETRIES = 2


def get_settings(user_id: int) -> dict:
    if user_id not in user_settings:
        user_settings[user_id] = {
            "video_model": "sora-2",
            "video_size": "1280x720",
            "video_duration": 4,
            "chat_provider": "openai",
            "chat_model": "gpt-4o",
            "image_model": "gpt-image-1",
            "image_quality": "medium",
            "image_count": 1,
            "tts_model": "tts-1",
            "tts_voice": "alloy",
            "tts_speed": 1.0,
            "custom_keys": [],
        }
    return user_settings[user_id]


def get_user_mode(user_id: int) -> int:
    return user_mode.get(user_id, MODE_AI)


def set_user_mode(user_id: int, mode: int):
    user_mode[user_id] = mode


# ── Key management ──
def get_user_keys_list(user_id: int) -> list[str]:
    s = get_settings(user_id)
    if s.get("custom_keys"):
        return list(s["custom_keys"])
    if global_keys_pool:
        return list(global_keys_pool)
    return []


def get_user_api_key(user_id: int) -> str | None:
    keys = get_user_keys_list(user_id)
    return random.choice(keys) if keys else None


def mask_key(key: str) -> str:
    if not key:
        return "not set"
    if len(key) <= 10:
        return "***" + key[-4:]
    return key[:8] + "..." + key[-4:]


def parse_keys(text: str) -> list[str]:
    keys = re.findall(r'sk-emergent-[\w]+', text)
    seen = set()
    unique = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


def is_balance_error(error: str) -> bool:
    error_lower = error.lower()
    keywords = ['balance', 'insufficient', 'quota', 'limit', 'exceeded',
                'billing', 'payment', 'credit', '402', '429', 'rate']
    return any(kw in error_lower for kw in keywords)


# ── Formatting utils ──
def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def format_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_expires(expires_str: str) -> str:
    try:
        from datetime import datetime
        if 'T' in expires_str:
            dt = datetime.fromisoformat(expires_str.replace('Z', '+00:00'))
            return dt.strftime('%d.%m.%Y %H:%M')
        return expires_str
    except Exception:
        return expires_str


# ── Progress & Status helpers ──
def progress_bar(pct: int, length: int = 10) -> str:
    filled = int(length * pct / 100)
    empty = length - filled
    bar = "\u2588" * filled + "\u2591" * empty
    return f"[{bar}] {pct}%"


def step_indicator(current: int, total: int, text: str) -> str:
    dots = "\u2501" * 2
    steps = ""
    for i in range(1, total + 1):
        if i < current:
            steps += "\u2714 "
        elif i == current:
            steps += f"\u25B6 "
        else:
            steps += "\u25CB "
    return f"{steps}\n<b>Шаг {current}/{total}:</b> {text}"


def status_box(title: str, lines: list[str], footer: str = "") -> str:
    border = "\u2500" * 22
    body = "\n".join(lines)
    result = f"<b>{title}</b>\n{border}\n{body}"
    if footer:
        result += f"\n{border}\n{footer}"
    return result


def is_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")
