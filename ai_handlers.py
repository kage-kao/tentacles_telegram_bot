"""AI Mode Router — Chat, Images, Video Gen, TTS, STT, Key management"""
import asyncio
import io
import os
import sys
import tempfile
import random
import uuid
import threading

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, FSInputFile,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from emergentintegrations.llm.openai.video_generation import OpenAIVideoGeneration
from emergentintegrations.llm.openai.image_generation import OpenAIImageGeneration
from emergentintegrations.llm.openai.text_to_speech import OpenAITextToSpeech
from emergentintegrations.llm.openai.speech_to_text import OpenAISpeechToText
from emergentintegrations.llm.chat import LlmChat, UserMessage

from config import (
    bot, logger, executor,
    get_settings, get_user_api_key, get_user_keys_list, mask_key, parse_keys,
    is_balance_error, global_keys_pool, ADMINS,
    CHAT_PROVIDERS, CHAT_PROVIDER_LABELS, CHAT_MODEL_LABELS,
    VIDEO_MODELS, VIDEO_SIZES, VIDEO_SIZE_LABELS, VIDEO_DURATIONS,
    IMAGE_QUALITY, TTS_MODELS, TTS_VOICES, MAX_RETRIES,
    get_user_mode, MODE_AI, ModeFilter,
    progress_bar, step_indicator, status_box,
)

ai_router = Router()
ai_router.message.filter(ModeFilter(MODE_AI))
ai_router.callback_query.filter(ModeFilter(MODE_AI))

# ── States ──
class AIStates(StatesGroup):
    waiting_for_key = State()
    waiting_for_bulk_keys = State()
    waiting_for_prompt = State()
    waiting_for_ref_image = State()
    waiting_for_prompt_with_ref = State()
    waiting_for_chat_message = State()
    waiting_for_image_prompt = State()
    waiting_for_tts_text = State()

# ── Storage ──
user_last_prompt: dict[int, str] = {}
user_ref_image: dict[int, str] = {}
user_chat_sessions: dict[int, LlmChat] = {}

# ── Thread-safe stdout capture ──
_stdout_lock = threading.Lock()

def capture_output(func, *args, **kwargs):
    with _stdout_lock:
        old_stdout = sys.stdout
        sys.stdout = buffer = io.StringIO()
        try:
            result = func(*args, **kwargs)
            output = buffer.getvalue()
            return result, output
        finally:
            sys.stdout = old_stdout


# ── Keyboards ──
def make_settings_hub_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Настройки Видео", callback_data="ai:settings:video")],
        [InlineKeyboardButton(text="Настройки Чата", callback_data="ai:settings:chat")],
        [InlineKeyboardButton(text="Настройки Изображений", callback_data="ai:settings:image")],
        [InlineKeyboardButton(text="Настройки TTS", callback_data="ai:settings:tts")],
        [InlineKeyboardButton(text="Главное меню", callback_data="global:main")],
    ])

def make_video_settings_keyboard(user_id):
    s = get_settings(user_id)
    rows = []
    rows.append([InlineKeyboardButton(
        text=(">> " if s["video_model"] == m else "   ") + m,
        callback_data=f"ai:vmodel:{m}",
    ) for m in VIDEO_MODELS])
    size_btns = [InlineKeyboardButton(
        text=(">> " if s["video_size"] == sz else "   ") + VIDEO_SIZE_LABELS[sz],
        callback_data=f"ai:vsize:{sz}",
    ) for sz in VIDEO_SIZES]
    rows.append(size_btns[:2])
    rows.append(size_btns[2:])
    rows.append([InlineKeyboardButton(
        text=(">> " if s["video_duration"] == d else "   ") + f"{d} сек",
        callback_data=f"ai:vdur:{d}",
    ) for d in VIDEO_DURATIONS])
    rows.append([
        InlineKeyboardButton(text="Назад", callback_data="ai:settings_hub"),
        InlineKeyboardButton(text="Главное меню", callback_data="global:main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def make_chat_settings_keyboard(user_id):
    s = get_settings(user_id)
    rows = []
    for provider, models in CHAT_PROVIDERS.items():
        label = CHAT_PROVIDER_LABELS[provider]
        for model in models:
            marker = ">> " if s["chat_provider"] == provider and s["chat_model"] == model else "   "
            rows.append([InlineKeyboardButton(
                text=f"{marker}{label}: {CHAT_MODEL_LABELS[model]}",
                callback_data=f"ai:chatmodel:{provider}:{model}",
            )])
    rows.append([InlineKeyboardButton(text="Сбросить историю", callback_data="ai:chat:reset")])
    rows.append([
        InlineKeyboardButton(text="Назад", callback_data="ai:settings_hub"),
        InlineKeyboardButton(text="Главное меню", callback_data="global:main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def make_image_settings_keyboard(user_id):
    s = get_settings(user_id)
    rows = [[InlineKeyboardButton(
        text=(">> " if s["image_quality"] == q else "   ") + q.capitalize(),
        callback_data=f"ai:imgquality:{q}",
    ) for q in IMAGE_QUALITY]]
    rows.append([
        InlineKeyboardButton(text="Назад", callback_data="ai:settings_hub"),
        InlineKeyboardButton(text="Главное меню", callback_data="global:main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def make_tts_settings_keyboard(user_id):
    s = get_settings(user_id)
    rows = [[InlineKeyboardButton(
        text=(">> " if s["tts_model"] == m else "   ") + m,
        callback_data=f"ai:ttsmodel:{m}",
    ) for m in TTS_MODELS]]
    voice_btns = [InlineKeyboardButton(
        text=(">> " if s["tts_voice"] == v else "   ") + v,
        callback_data=f"ai:ttsvoice:{v}",
    ) for v in TTS_VOICES]
    rows.append(voice_btns[:3])
    rows.append(voice_btns[3:6])
    rows.append(voice_btns[6:])
    rows.append([
        InlineKeyboardButton(text="Назад", callback_data="ai:settings_hub"),
        InlineKeyboardButton(text="Главное меню", callback_data="global:main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def make_key_keyboard(user_id):
    s = get_settings(user_id)
    user_keys_count = len(s.get("custom_keys", []))
    rows = [
        [InlineKeyboardButton(text="Добавить ключ", callback_data="ai:key:add_one")],
        [InlineKeyboardButton(text="Добавить несколько", callback_data="ai:key:add_bulk")],
    ]
    if user_keys_count > 0:
        rows.append([InlineKeyboardButton(text=f"Мои ключи ({user_keys_count})", callback_data="ai:key:list")])
        rows.append([InlineKeyboardButton(text="Удалить все", callback_data="ai:key:clear")])
    rows.append([
        InlineKeyboardButton(text="Главное меню", callback_data="global:main"),
        InlineKeyboardButton(text="Закрыть", callback_data="ai:key:close"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def make_admin_key_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить в глобальный пул", callback_data="ai:admin:add_bulk")],
        [InlineKeyboardButton(text=f"Глобальный пул ({len(global_keys_pool)})", callback_data="ai:admin:list")],
        [InlineKeyboardButton(text="Очистить глобальный пул", callback_data="ai:admin:clear")],
        [InlineKeyboardButton(text="Закрыть", callback_data="ai:key:close")],
    ])

def make_quick_gen_keyboard(has_last_prompt=True):
    rows = []
    if has_last_prompt:
        rows.append([InlineKeyboardButton(text="Повторить", callback_data="ai:quick:retry")])
    rows.append([
        InlineKeyboardButton(text="Настройки", callback_data="ai:settings_hub"),
        InlineKeyboardButton(text="Ключи", callback_data="ai:menu:keys"),
    ])
    rows.append([
        InlineKeyboardButton(text="Видео", callback_data="ai:menu:gen_text"),
        InlineKeyboardButton(text="Меню", callback_data="global:main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def make_after_chat_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Сбросить историю", callback_data="ai:chat:reset"),
            InlineKeyboardButton(text="Настройки чата", callback_data="ai:settings:chat"),
        ],
        [InlineKeyboardButton(text="Главное меню", callback_data="global:main")],
    ])

def make_after_image_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Ещё картинку", callback_data="ai:menu:image"),
            InlineKeyboardButton(text="Настройки", callback_data="ai:settings:image"),
        ],
        [InlineKeyboardButton(text="Главное меню", callback_data="global:main")],
    ])

def make_after_tts_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Ещё озвучку", callback_data="ai:menu:tts"),
            InlineKeyboardButton(text="Настройки TTS", callback_data="ai:settings:tts"),
        ],
        [InlineKeyboardButton(text="Главное меню", callback_data="global:main")],
    ])

# ── Text helpers ──
def key_status_text(user_id):
    s = get_settings(user_id)
    user_keys = s.get("custom_keys", [])
    lines = ["<b>Universal Keys</b>\n"]
    lines.append(f"Ваши ключи: {len(user_keys)}" if user_keys else "Ваши ключи: нет")
    lines.append(f"Глобальный пул: {len(global_keys_pool)}")
    if user_keys:
        lines.append("\nИспользуются ваши ключи")
    elif global_keys_pool:
        lines.append("\nИспользуется глобальный пул")
    else:
        lines.append("\nНет доступных ключей!")
    lines.append("\nПолучить ключ: <a href='https://emergent.sh'>emergent.sh</a>")
    return "\n".join(lines)

def full_status_text(user_id):
    s = get_settings(user_id)
    user_keys = len(s.get("custom_keys", []))
    if user_keys:
        key_info = f"{user_keys} персональных"
    elif global_keys_pool:
        key_info = f"глобальный ({len(global_keys_pool)})"
    else:
        key_info = "НЕТ КЛЮЧЕЙ - /key"
    chat_label = CHAT_MODEL_LABELS.get(s["chat_model"], s["chat_model"])
    return (
        f"<b>Текущая конфигурация</b>\n\n"
        f"<b>Чат:</b> <code>{chat_label}</code>\n"
        f"<b>Изображения:</b> <code>{s['image_model']}</code> ({s['image_quality']})\n"
        f"<b>Видео:</b> <code>{s['video_model']}</code> {s['video_size']} {s['video_duration']}сек\n"
        f"<b>TTS:</b> <code>{s['tts_model']}</code> ({s['tts_voice']})\n\n"
        f"Ключи: {key_info}"
    )

# ── Video gen sync ──
def generate_video_sync(prompt, model, size, duration, output_path, api_key, ref_image_path=None):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            video_gen = OpenAIVideoGeneration(api_key=api_key)
            kwargs = {"prompt": prompt, "model": model, "size": size, "duration": duration, "max_wait_time": 900}
            if ref_image_path and os.path.exists(ref_image_path):
                kwargs["image_path"] = ref_image_path
                ext = os.path.splitext(ref_image_path)[1].lower()
                mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
                kwargs["mime_type"] = mime_map.get(ext, "image/jpeg")
            video_bytes, lib_output = capture_output(video_gen.text_to_video, **kwargs)
            if video_bytes and len(video_bytes) > 1000:
                video_gen.save_video(video_bytes, output_path)
                if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                    return output_path, None
                last_error = "Файл видео пустой"
            else:
                last_error = lib_output or "API вернул пустой ответ"
        except ValueError as exc:
            return None, str(exc)
        except Exception as exc:
            last_error = str(exc)
    return None, last_error

# ══════════════ COMMANDS ══════════════

@ai_router.message(Command("chat"))
async def cmd_chat(message: types.Message, state: FSMContext):
    keys = get_user_keys_list(message.from_user.id)
    if not keys:
        await message.answer("<b>Нет ключей!</b>\nДобавьте через /key", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Добавить ключ", callback_data="ai:key:add_one")]]))
        return
    s = get_settings(message.from_user.id)
    label = CHAT_MODEL_LABELS.get(s["chat_model"], s["chat_model"])
    await state.set_state(AIStates.waiting_for_chat_message)
    await message.answer(
        f"<b>AI Чат</b>\nМодель: <code>{label}</code>\n\nОтправьте сообщение.\n/cancel для выхода.",
        parse_mode="HTML")

@ai_router.message(Command("image"))
async def cmd_image(message: types.Message, state: FSMContext):
    keys = get_user_keys_list(message.from_user.id)
    if not keys:
        await message.answer("<b>Нет ключей!</b>\nДобавьте через /key", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Добавить ключ", callback_data="ai:key:add_one")]]))
        return
    s = get_settings(message.from_user.id)
    await state.set_state(AIStates.waiting_for_image_prompt)
    await message.answer(
        f"<b>Генерация изображений</b>\nМодель: <code>{s['image_model']}</code> | Качество: <code>{s['image_quality']}</code>\n\nОтправьте описание.\n/cancel для отмены.",
        parse_mode="HTML")

@ai_router.message(Command("gen"))
async def cmd_gen(message: types.Message, state: FSMContext):
    keys = get_user_keys_list(message.from_user.id)
    if not keys:
        await message.answer("<b>Нет ключей!</b>\nДобавьте через /key", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Добавить ключ", callback_data="ai:key:add_one")]]))
        return
    s = get_settings(message.from_user.id)
    await state.set_state(AIStates.waiting_for_prompt)
    await message.answer(
        f"<b>Генерация видео (AI)</b>\n<code>{s['video_model']}</code> | <code>{s['video_size']}</code> | <code>{s['video_duration']} сек</code>\n\nОтправьте промпт.\n/cancel для отмены.",
        parse_mode="HTML")

@ai_router.message(Command("genref"))
async def cmd_genref(message: types.Message, state: FSMContext):
    keys = get_user_keys_list(message.from_user.id)
    if not keys:
        await message.answer("<b>Нет ключей!</b>\nДобавьте через /key", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Добавить ключ", callback_data="ai:key:add_one")]]))
        return
    s = get_settings(message.from_user.id)
    await state.set_state(AIStates.waiting_for_ref_image)
    await message.answer(
        f"<b>Видео с референс фото</b>\n<code>{s['video_model']}</code> | <code>{s['video_size']}</code> | <code>{s['video_duration']} сек</code>\n\nОтправьте фото.\n/cancel для отмены.",
        parse_mode="HTML")

@ai_router.message(Command("tts"))
async def cmd_tts(message: types.Message, state: FSMContext):
    keys = get_user_keys_list(message.from_user.id)
    if not keys:
        await message.answer("<b>Нет ключей!</b>\nДобавьте через /key", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Добавить ключ", callback_data="ai:key:add_one")]]))
        return
    s = get_settings(message.from_user.id)
    await state.set_state(AIStates.waiting_for_tts_text)
    await message.answer(
        f"<b>Озвучка текста (TTS)</b>\nМодель: <code>{s['tts_model']}</code> | Голос: <code>{s['tts_voice']}</code>\n\nОтправьте текст (до 4096 символов).\n/cancel для отмены.",
        parse_mode="HTML")

@ai_router.message(Command("key"))
async def cmd_key(message: types.Message):
    await message.answer(key_status_text(message.from_user.id), parse_mode="HTML",
        reply_markup=make_key_keyboard(message.from_user.id), disable_web_page_preview=True)

@ai_router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in ADMINS:
        await message.answer("Доступ запрещен.")
        return
    await message.answer(
        f"<b>Админ-панель</b>\n\nГлобальный пул: {len(global_keys_pool)} ключей",
        parse_mode="HTML", reply_markup=make_admin_key_keyboard())

# ══════════════ DO FUNCTIONS ══════════════

async def _do_chat(message, user_id, text):
    s = get_settings(user_id)
    api_key = get_user_api_key(user_id)
    if not api_key:
        await message.answer("<b>Нет ключей!</b> /key", parse_mode="HTML")
        return
    if user_id not in user_chat_sessions:
        chat = LlmChat(api_key=api_key, session_id=str(uuid.uuid4()),
            system_message="Ты полезный AI-ассистент. Отвечай на русском языке, если пользователь пишет на русском.")
        chat.with_model(s["chat_provider"], s["chat_model"])
        user_chat_sessions[user_id] = chat
    else:
        chat = user_chat_sessions[user_id]
        chat.api_key = api_key
        chat.with_model(s["chat_provider"], s["chat_model"])
    label = CHAT_MODEL_LABELS.get(s["chat_model"], s["chat_model"])
    thinking = await message.answer(
        status_box("AI Chat", [
            f"Модель: <code>{label}</code>",
            f"",
            f"\u23f3 Генерирую ответ...",
        ]),
        parse_mode="HTML",
    )
    import time
    t0 = time.time()
    try:
        response = await chat.send_message(UserMessage(text=text))
        elapsed = time.time() - t0
        if response:
            if len(response) > 4000:
                chunks = [response[i:i+4000] for i in range(0, len(response), 4000)]
                for i, chunk in enumerate(chunks):
                    if i == len(chunks) - 1:
                        await message.answer(chunk, reply_markup=make_after_chat_keyboard())
                    else:
                        await message.answer(chunk)
            else:
                await message.answer(response, reply_markup=make_after_chat_keyboard())
        else:
            await message.answer("Пустой ответ от AI.", reply_markup=make_after_chat_keyboard())
        try: await thinking.delete()
        except Exception: pass
    except Exception as e:
        elapsed = time.time() - t0
        logger.error(f"Ошибка чата: {e}")
        await thinking.edit_text(
            status_box("\u274c Ошибка чата", [
                f"Модель: <code>{label}</code>",
                f"Время: {elapsed:.1f}с",
                f"",
                f"<code>{str(e)[:400]}</code>",
            ]),
            parse_mode="HTML",
            reply_markup=make_after_chat_keyboard(),
        )

async def _do_image(message, user_id, prompt):
    s = get_settings(user_id)
    api_key = get_user_api_key(user_id)
    if not api_key:
        await message.answer("<b>Нет ключей!</b> /key", parse_mode="HTML")
        return
    import time
    t0 = time.time()
    status = await message.answer(
        status_box("Image Generation", [
            f"Модель: <code>{s['image_model']}</code>",
            f"Качество: <code>{s['image_quality']}</code>",
            f"Промпт: <i>{prompt[:120]}{'...' if len(prompt)>120 else ''}</i>",
            f"",
            step_indicator(1, 2, "Генерация изображения..."),
        ]),
        parse_mode="HTML",
    )
    try:
        img_gen = OpenAIImageGeneration(api_key=api_key)
        images = await img_gen.generate_images(prompt=prompt, model=s["image_model"], number_of_images=s.get("image_count", 1), quality=s["image_quality"])
        elapsed = time.time() - t0
        await status.edit_text(
            status_box("Image Generation", [
                step_indicator(2, 2, "Отправка..."),
                f"Время: {elapsed:.1f}с",
            ]),
            parse_mode="HTML",
        )
        from aiogram.types import BufferedInputFile
        for i, img_bytes in enumerate(images):
            photo = BufferedInputFile(img_bytes, filename=f"image_{i}.png")
            caption = (
                status_box("\u2714 Изображение готово", [
                    f"Модель: <code>{s['image_model']}</code> | Качество: <code>{s['image_quality']}</code>",
                    f"Время: <b>{elapsed:.1f}с</b>",
                    f"Промпт: <i>{prompt[:150]}</i>",
                ])
            ) if i == 0 else None
            await message.answer_photo(photo=photo, caption=caption, parse_mode="HTML",
                reply_markup=make_after_image_keyboard() if i == len(images) - 1 else None)
        try: await status.delete()
        except Exception: pass
    except Exception as e:
        elapsed = time.time() - t0
        logger.error(f"Ошибка генерации изображения: {e}")
        await status.edit_text(
            status_box("\u274c Ошибка генерации", [
                f"Модель: <code>{s['image_model']}</code>",
                f"Время: {elapsed:.1f}с",
                f"",
                f"<code>{str(e)[:400]}</code>",
            ]),
            parse_mode="HTML",
            reply_markup=make_after_image_keyboard(),
        )

async def _do_tts(message, user_id, text):
    s = get_settings(user_id)
    api_key = get_user_api_key(user_id)
    if not api_key:
        await message.answer("<b>Нет ключей!</b> /key", parse_mode="HTML")
        return
    if len(text) > 4096:
        await message.answer("Текст слишком длинный! Максимум 4096 символов.")
        return
    import time
    t0 = time.time()
    status = await message.answer(
        status_box("Text-to-Speech", [
            f"Модель: <code>{s['tts_model']}</code> | Голос: <code>{s['tts_voice']}</code>",
            f"Символов: {len(text)}",
            f"",
            step_indicator(1, 2, "Синтезирую речь..."),
        ]),
        parse_mode="HTML",
    )
    try:
        tts = OpenAITextToSpeech(api_key=api_key)
        audio_bytes = await tts.generate_speech(text=text, model=s["tts_model"], voice=s["tts_voice"], speed=s.get("tts_speed", 1.0), response_format="mp3")
        elapsed = time.time() - t0
        from aiogram.types import BufferedInputFile
        audio_file = BufferedInputFile(audio_bytes, filename="speech.mp3")
        caption = status_box("\u2714 Озвучка готова", [
            f"Модель: <code>{s['tts_model']}</code> | Голос: <code>{s['tts_voice']}</code>",
            f"Время: <b>{elapsed:.1f}с</b> | {len(audio_bytes) // 1024} KB",
        ])
        await message.answer_voice(voice=audio_file, caption=caption, parse_mode="HTML", reply_markup=make_after_tts_keyboard())
        try: await status.delete()
        except Exception: pass
    except Exception as e:
        elapsed = time.time() - t0
        logger.error(f"Ошибка TTS: {e}")
        await status.edit_text(
            status_box("\u274c Ошибка TTS", [
                f"Модель: <code>{s['tts_model']}</code>",
                f"Время: {elapsed:.1f}с",
                f"",
                f"<code>{str(e)[:400]}</code>",
            ]),
            parse_mode="HTML",
            reply_markup=make_after_tts_keyboard(),
        )

async def _do_stt(message, user_id, file_path):
    api_key = get_user_api_key(user_id)
    if not api_key:
        await message.answer("<b>Нет ключей!</b> /key", parse_mode="HTML")
        return
    import time
    t0 = time.time()
    status = await message.answer(
        status_box("Speech-to-Text", [
            f"Модель: <code>whisper-1</code>",
            f"",
            step_indicator(1, 2, "Распознаю речь..."),
        ]),
        parse_mode="HTML",
    )
    try:
        stt = OpenAISpeechToText(api_key=api_key)
        result = await stt.transcribe(file=file_path, model="whisper-1", response_format="json")
        text = result.text if hasattr(result, 'text') else result.get('text', str(result)) if isinstance(result, dict) else str(result)
        elapsed = time.time() - t0
        if text:
            await status.edit_text(
                status_box("\u2714 Распознано", [
                    f"Время: <b>{elapsed:.1f}с</b> | Символов: {len(text)}",
                    f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
                    text[:3500],
                ]),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Главное меню", callback_data="global:main")]]),
            )
        else:
            await status.edit_text(
                status_box("\u26a0 STT", ["Не удалось распознать речь."]),
                parse_mode="HTML",
            )
    except Exception as e:
        elapsed = time.time() - t0
        logger.error(f"Ошибка STT: {e}")
        await status.edit_text(
            status_box("\u274c Ошибка STT", [
                f"Время: {elapsed:.1f}с",
                f"<code>{str(e)[:400]}</code>",
            ]),
            parse_mode="HTML",
        )
    finally:
        if os.path.exists(file_path):
            os.unlink(file_path)

async def _do_generate(message, user_id, prompt, ref_image_path=None):
    s = get_settings(user_id)
    all_keys = get_user_keys_list(user_id)
    if not all_keys:
        await message.answer("<b>Нет ключей!</b> /key", parse_mode="HTML")
        return
    user_last_prompt[user_id] = prompt
    import time
    t0 = time.time()
    ref_label = " + Ref image" if ref_image_path else ""
    status = await message.answer(
        status_box(f"Video Generation{ref_label}", [
            f"Модель: <code>{s['video_model']}</code>",
            f"Размер: <code>{s['video_size']}</code> | Длина: <code>{s['video_duration']} сек</code>",
            f"Промпт: <i>{prompt[:100]}{'...' if len(prompt)>100 else ''}</i>",
            f"",
            step_indicator(1, 3, "Отправка запроса в API..."),
            f"",
            f"\u23f3 Ожидание: <b>2\u201310 мин</b>",
        ]),
        parse_mode="HTML",
    )
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name
    keys_to_try = list(all_keys)
    random.shuffle(keys_to_try)
    last_error = None
    try:
        loop = asyncio.get_running_loop()
        for i, api_key in enumerate(keys_to_try):
            elapsed = time.time() - t0
            if i > 0:
                try:
                    await status.edit_text(
                        status_box(f"Video Generation", [
                            f"Модель: <code>{s['video_model']}</code>",
                            f"Попытка: <b>{i+1}/{len(keys_to_try)}</b>",
                            f"Время: {elapsed:.0f}с",
                            f"",
                            step_indicator(1, 3, "Повторная отправка..."),
                        ]),
                        parse_mode="HTML",
                    )
                except Exception: pass
            result, error = await loop.run_in_executor(executor, generate_video_sync, prompt, s["video_model"], s["video_size"], s["video_duration"], tmp_path, api_key, ref_image_path)
            if result and os.path.exists(result) and os.path.getsize(result) > 1000:
                elapsed = time.time() - t0
                file_mb = os.path.getsize(result) / (1024*1024)
                try:
                    await status.edit_text(
                        status_box("Video Generation", [
                            step_indicator(3, 3, "Отправка видео..."),
                            f"Время: {elapsed:.0f}с | Размер: {file_mb:.1f} МБ",
                        ]),
                        parse_mode="HTML",
                    )
                except Exception: pass
                caption = status_box("\u2714 Видео готово", [
                    f"Модель: <code>{s['video_model']}</code> | {s['video_size']} | {s['video_duration']} сек",
                    f"Время: <b>{elapsed:.0f}с</b> | Размер: <b>{file_mb:.1f} МБ</b>",
                    f"Промпт: <i>{prompt[:150]}</i>",
                ])
                try:
                    await message.answer_video(video=FSInputFile(result),
                        caption=caption,
                        parse_mode="HTML", reply_markup=make_quick_gen_keyboard())
                except Exception:
                    await message.answer_document(document=FSInputFile(result),
                        caption=caption, parse_mode="HTML", reply_markup=make_quick_gen_keyboard())
                try: await status.delete()
                except Exception: pass
                return
            last_error = error
            if error and not is_balance_error(error):
                break
        elapsed = time.time() - t0
        await status.edit_text(
            status_box("\u274c Ошибка генерации видео", [
                f"Модель: <code>{s['video_model']}</code>",
                f"Время: {elapsed:.0f}с | Попыток: {len(keys_to_try)}",
                f"",
                f"<code>{str(last_error)[:300]}</code>",
            ]),
            parse_mode="HTML",
            reply_markup=make_quick_gen_keyboard(),
        )
    except Exception as exc:
        elapsed = time.time() - t0
        await status.edit_text(
            status_box("\u274c Ошибка", [
                f"Время: {elapsed:.0f}с",
                f"<code>{str(exc)[:300]}</code>",
            ]),
            parse_mode="HTML",
            reply_markup=make_quick_gen_keyboard(),
        )
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

# ══════════════ CALLBACKS ══════════════

@ai_router.callback_query(F.data == "ai:settings_hub")
async def cb_settings_hub(call: CallbackQuery):
    await call.message.edit_text("<b>Настройки AI</b>\n\nВыберите раздел:", parse_mode="HTML", reply_markup=make_settings_hub_keyboard())
    await call.answer()

@ai_router.callback_query(F.data == "ai:menu:keys")
async def cb_menu_keys(call: CallbackQuery):
    await call.message.edit_text(key_status_text(call.from_user.id), parse_mode="HTML", reply_markup=make_key_keyboard(call.from_user.id), disable_web_page_preview=True)
    await call.answer()

@ai_router.callback_query(F.data == "ai:menu:status")
async def cb_menu_status(call: CallbackQuery):
    await call.message.edit_text(full_status_text(call.from_user.id), parse_mode="HTML")
    await call.answer()

# Chat menu callbacks
@ai_router.callback_query(F.data == "ai:menu:chat")
async def cb_menu_chat(call: CallbackQuery, state: FSMContext):
    keys = get_user_keys_list(call.from_user.id)
    if not keys:
        await call.message.edit_text("<b>Нет ключей!</b>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Добавить ключ", callback_data="ai:key:add_one")], [InlineKeyboardButton(text="Меню", callback_data="global:main")]]))
        await call.answer()
        return
    s = get_settings(call.from_user.id)
    label = CHAT_MODEL_LABELS.get(s["chat_model"], s["chat_model"])
    await state.set_state(AIStates.waiting_for_chat_message)
    await call.message.edit_text(f"<b>AI Чат</b>\nМодель: <code>{label}</code>\n\nОтправьте сообщение.\n/cancel для выхода.", parse_mode="HTML")
    await call.answer()

@ai_router.callback_query(F.data == "ai:menu:image")
async def cb_menu_image(call: CallbackQuery, state: FSMContext):
    keys = get_user_keys_list(call.from_user.id)
    if not keys:
        await call.message.edit_text("<b>Нет ключей!</b>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Добавить ключ", callback_data="ai:key:add_one")], [InlineKeyboardButton(text="Меню", callback_data="global:main")]]))
        await call.answer()
        return
    s = get_settings(call.from_user.id)
    await state.set_state(AIStates.waiting_for_image_prompt)
    await call.message.edit_text(f"<b>Генерация изображений</b>\n<code>{s['image_model']}</code> | <code>{s['image_quality']}</code>\n\nОтправьте описание.\n/cancel для отмены.", parse_mode="HTML")
    await call.answer()

@ai_router.callback_query(F.data == "ai:menu:gen_text")
async def cb_menu_gen_text(call: CallbackQuery, state: FSMContext):
    keys = get_user_keys_list(call.from_user.id)
    if not keys:
        await call.message.edit_text("<b>Нет ключей!</b>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Добавить ключ", callback_data="ai:key:add_one")], [InlineKeyboardButton(text="Меню", callback_data="global:main")]]))
        await call.answer()
        return
    s = get_settings(call.from_user.id)
    await state.set_state(AIStates.waiting_for_prompt)
    await call.message.edit_text(f"<b>Генерация видео</b>\n<code>{s['video_model']}</code> | <code>{s['video_size']}</code> | <code>{s['video_duration']} сек</code>\n\nОтправьте промпт.\n/cancel для отмены.", parse_mode="HTML")
    await call.answer()

@ai_router.callback_query(F.data == "ai:menu:gen_ref")
async def cb_menu_gen_ref(call: CallbackQuery, state: FSMContext):
    keys = get_user_keys_list(call.from_user.id)
    if not keys:
        await call.message.edit_text("<b>Нет ключей!</b>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Добавить ключ", callback_data="ai:key:add_one")], [InlineKeyboardButton(text="Меню", callback_data="global:main")]]))
        await call.answer()
        return
    await state.set_state(AIStates.waiting_for_ref_image)
    await call.message.edit_text("<b>Видео с референс фото</b>\nОтправьте фото.\n/cancel для отмены.", parse_mode="HTML")
    await call.answer()

@ai_router.callback_query(F.data == "ai:menu:tts")
async def cb_menu_tts(call: CallbackQuery, state: FSMContext):
    keys = get_user_keys_list(call.from_user.id)
    if not keys:
        await call.message.edit_text("<b>Нет ключей!</b>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Добавить ключ", callback_data="ai:key:add_one")], [InlineKeyboardButton(text="Меню", callback_data="global:main")]]))
        await call.answer()
        return
    s = get_settings(call.from_user.id)
    await state.set_state(AIStates.waiting_for_tts_text)
    await call.message.edit_text(f"<b>Озвучка TTS</b>\nГолос: <code>{s['tts_voice']}</code> | Модель: <code>{s['tts_model']}</code>\n\nОтправьте текст.\n/cancel для отмены.", parse_mode="HTML")
    await call.answer()

@ai_router.callback_query(F.data == "ai:menu:stt_info")
async def cb_menu_stt_info(call: CallbackQuery):
    await call.message.edit_text("<b>Распознавание речи (STT)</b>\n\nОтправьте голосовое сообщение или аудиофайл.\nWhisper-1 | до 25 МБ", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Главное меню", callback_data="global:main")]]))
    await call.answer()

# Settings callbacks
@ai_router.callback_query(F.data == "ai:settings:video")
async def cb_settings_video(call: CallbackQuery):
    s = get_settings(call.from_user.id)
    await call.message.edit_text(f"<b>Настройки видео</b>\nМодель: <code>{s['video_model']}</code> | Размер: <code>{s['video_size']}</code> | <code>{s['video_duration']} сек</code>", parse_mode="HTML", reply_markup=make_video_settings_keyboard(call.from_user.id))
    await call.answer()

@ai_router.callback_query(F.data.startswith("ai:vmodel:"))
async def cb_vmodel(call: CallbackQuery):
    model = call.data.split(":")[-1]
    if model in VIDEO_MODELS:
        get_settings(call.from_user.id)["video_model"] = model
        await call.message.edit_reply_markup(reply_markup=make_video_settings_keyboard(call.from_user.id))
        await call.answer(f"Модель: {model}")

@ai_router.callback_query(F.data.startswith("ai:vsize:"))
async def cb_vsize(call: CallbackQuery):
    size = call.data.split(":")[-1]
    if size in VIDEO_SIZES:
        get_settings(call.from_user.id)["video_size"] = size
        await call.message.edit_reply_markup(reply_markup=make_video_settings_keyboard(call.from_user.id))
        await call.answer(f"Размер: {size}")

@ai_router.callback_query(F.data.startswith("ai:vdur:"))
async def cb_vdur(call: CallbackQuery):
    try: dur = int(call.data.split(":")[-1])
    except ValueError: return
    if dur in VIDEO_DURATIONS:
        get_settings(call.from_user.id)["video_duration"] = dur
        await call.message.edit_reply_markup(reply_markup=make_video_settings_keyboard(call.from_user.id))
        await call.answer(f"Длительность: {dur} сек")

@ai_router.callback_query(F.data == "ai:settings:chat")
async def cb_settings_chat(call: CallbackQuery):
    s = get_settings(call.from_user.id)
    label = CHAT_MODEL_LABELS.get(s["chat_model"], s["chat_model"])
    await call.message.edit_text(f"<b>Настройки чата</b>\nТекущая модель: <code>{label}</code>", parse_mode="HTML", reply_markup=make_chat_settings_keyboard(call.from_user.id))
    await call.answer()

@ai_router.callback_query(F.data.startswith("ai:chatmodel:"))
async def cb_chatmodel(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) == 4:
        provider, model = parts[2], parts[3]
        if provider in CHAT_PROVIDERS and model in CHAT_PROVIDERS[provider]:
            s = get_settings(call.from_user.id)
            s["chat_provider"] = provider
            s["chat_model"] = model
            if call.from_user.id in user_chat_sessions:
                del user_chat_sessions[call.from_user.id]
            label = CHAT_MODEL_LABELS.get(model, model)
            await call.message.edit_text(f"<b>Настройки чата</b>\nТекущая модель: <code>{label}</code>", parse_mode="HTML", reply_markup=make_chat_settings_keyboard(call.from_user.id))
            await call.answer(f"Модель: {label}")

@ai_router.callback_query(F.data == "ai:chat:reset")
async def cb_chat_reset(call: CallbackQuery):
    if call.from_user.id in user_chat_sessions:
        del user_chat_sessions[call.from_user.id]
    await call.answer("История чата сброшена!")

@ai_router.callback_query(F.data == "ai:settings:image")
async def cb_settings_image(call: CallbackQuery):
    s = get_settings(call.from_user.id)
    await call.message.edit_text(f"<b>Настройки изображений</b>\nМодель: <code>{s['image_model']}</code> | Качество: <code>{s['image_quality']}</code>", parse_mode="HTML", reply_markup=make_image_settings_keyboard(call.from_user.id))
    await call.answer()

@ai_router.callback_query(F.data.startswith("ai:imgquality:"))
async def cb_imgquality(call: CallbackQuery):
    q = call.data.split(":")[-1]
    if q in IMAGE_QUALITY:
        get_settings(call.from_user.id)["image_quality"] = q
        await call.message.edit_reply_markup(reply_markup=make_image_settings_keyboard(call.from_user.id))
        await call.answer(f"Качество: {q}")

@ai_router.callback_query(F.data == "ai:settings:tts")
async def cb_settings_tts(call: CallbackQuery):
    s = get_settings(call.from_user.id)
    await call.message.edit_text(f"<b>Настройки TTS</b>\nМодель: <code>{s['tts_model']}</code> | Голос: <code>{s['tts_voice']}</code>", parse_mode="HTML", reply_markup=make_tts_settings_keyboard(call.from_user.id))
    await call.answer()

@ai_router.callback_query(F.data.startswith("ai:ttsmodel:"))
async def cb_ttsmodel(call: CallbackQuery):
    m = call.data.split(":")[-1]
    if m in TTS_MODELS:
        get_settings(call.from_user.id)["tts_model"] = m
        await call.message.edit_reply_markup(reply_markup=make_tts_settings_keyboard(call.from_user.id))
        await call.answer(f"Модель: {m}")

@ai_router.callback_query(F.data.startswith("ai:ttsvoice:"))
async def cb_ttsvoice(call: CallbackQuery):
    v = call.data.split(":")[-1]
    if v in TTS_VOICES:
        get_settings(call.from_user.id)["tts_voice"] = v
        await call.message.edit_reply_markup(reply_markup=make_tts_settings_keyboard(call.from_user.id))
        await call.answer(f"Голос: {v}")

# Key callbacks
@ai_router.callback_query(F.data == "ai:key:add_one")
async def cb_key_add_one(call: CallbackQuery, state: FSMContext):
    await state.set_state(AIStates.waiting_for_key)
    await call.message.edit_text("<b>Добавить ключ</b>\n\nОтправьте Emergent Universal Key.\nФормат: <code>sk-emergent-XXXXXXXX</code>\n\n/cancel для отмены.", parse_mode="HTML")
    await call.answer()

@ai_router.callback_query(F.data == "ai:key:add_bulk")
async def cb_key_add_bulk(call: CallbackQuery, state: FSMContext):
    await state.set_state(AIStates.waiting_for_bulk_keys)
    await state.update_data(target="user")
    await call.message.edit_text("<b>Добавить несколько ключей</b>\n\nОтправьте ключи.\n/cancel для отмены.", parse_mode="HTML")
    await call.answer()

@ai_router.callback_query(F.data == "ai:key:list")
async def cb_key_list(call: CallbackQuery):
    s = get_settings(call.from_user.id)
    keys = s.get("custom_keys", [])
    if not keys:
        await call.answer("Нет ключей")
        return
    lines = ["<b>Ваши ключи:</b>\n"]
    for i, key in enumerate(keys, 1):
        lines.append(f"{i}. <code>{mask_key(key)}</code>")
    await call.message.edit_text("\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="ai:menu:keys")]]))
    await call.answer()

@ai_router.callback_query(F.data == "ai:key:clear")
async def cb_key_clear(call: CallbackQuery):
    get_settings(call.from_user.id)["custom_keys"] = []
    await call.message.edit_text(key_status_text(call.from_user.id), parse_mode="HTML", reply_markup=make_key_keyboard(call.from_user.id), disable_web_page_preview=True)
    await call.answer("Все ключи удалены!")

@ai_router.callback_query(F.data == "ai:key:close")
async def cb_key_close(call: CallbackQuery):
    try: await call.message.delete()
    except Exception: pass
    await call.answer()

@ai_router.callback_query(F.data == "ai:admin:add_bulk")
async def cb_admin_add_bulk(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMINS:
        await call.answer("Доступ запрещен"); return
    await state.set_state(AIStates.waiting_for_bulk_keys)
    await state.update_data(target="global")
    await call.message.edit_text("<b>Добавить в глобальный пул</b>\n\nОтправьте ключи.\n/cancel для отмены.", parse_mode="HTML")
    await call.answer()

@ai_router.callback_query(F.data == "ai:admin:list")
async def cb_admin_list(call: CallbackQuery):
    if call.from_user.id not in ADMINS:
        await call.answer("Доступ запрещен"); return
    if not global_keys_pool:
        await call.answer("Пул пуст"); return
    lines = ["<b>Глобальный пул:</b>\n"]
    for i, key in enumerate(global_keys_pool, 1):
        lines.append(f"{i}. <code>{mask_key(key)}</code>")
    await call.message.edit_text("\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="ai:admin:back")]]))
    await call.answer()

@ai_router.callback_query(F.data == "ai:admin:clear")
async def cb_admin_clear(call: CallbackQuery):
    if call.from_user.id not in ADMINS:
        await call.answer("Доступ запрещен"); return
    global_keys_pool.clear()
    await call.message.edit_text("<b>Админ-панель</b>\n\nПул очищен! 0 ключей", parse_mode="HTML", reply_markup=make_admin_key_keyboard())
    await call.answer("Очищено!")

@ai_router.callback_query(F.data == "ai:admin:back")
async def cb_admin_back(call: CallbackQuery):
    await call.message.edit_text(f"<b>Админ-панель</b>\n\nГлобальный пул: {len(global_keys_pool)} ключей", parse_mode="HTML", reply_markup=make_admin_key_keyboard())
    await call.answer()

@ai_router.callback_query(F.data == "ai:quick:retry")
async def cb_quick_retry(call: CallbackQuery):
    prompt = user_last_prompt.get(call.from_user.id)
    if not prompt:
        await call.answer("Нет предыдущего промпта"); return
    await call.answer("Повтор...")
    try: await call.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    await _do_generate(call.message, call.from_user.id, prompt)

# ══════════════ FSM HANDLERS ══════════════

@ai_router.message(AIStates.waiting_for_key)
async def process_key_input(message: types.Message, state: FSMContext):
    key = message.text.strip()
    if not key.startswith("sk-emergent-") or len(key) < 20:
        await message.answer("Неверный формат. <code>sk-emergent-...</code>\n/cancel", parse_mode="HTML")
        return
    s = get_settings(message.from_user.id)
    if key not in s.get("custom_keys", []):
        s.setdefault("custom_keys", []).append(key)
    await state.clear()
    try: await message.delete()
    except Exception: pass
    await message.answer(f"Ключ добавлен! Всего: {len(s['custom_keys'])}")

@ai_router.message(AIStates.waiting_for_bulk_keys)
async def process_bulk_keys(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("target", "user")
    keys = parse_keys(message.text)
    if not keys:
        await message.answer("Ключи не найдены. <code>sk-emergent-...</code>\n/cancel", parse_mode="HTML")
        return
    added = 0
    if target == "global" and message.from_user.id in ADMINS:
        for key in keys:
            if key not in global_keys_pool:
                global_keys_pool.append(key); added += 1
        total = len(global_keys_pool)
    else:
        s = get_settings(message.from_user.id)
        s.setdefault("custom_keys", [])
        for key in keys:
            if key not in s["custom_keys"]:
                s["custom_keys"].append(key); added += 1
        total = len(s["custom_keys"])
    await state.clear()
    try: await message.delete()
    except Exception: pass
    await message.answer(f"<b>Ключи добавлены!</b>\nНайдено: {len(keys)} | Новых: {added} | Всего: {total}", parse_mode="HTML")

@ai_router.message(AIStates.waiting_for_prompt)
async def process_video_prompt(message: types.Message, state: FSMContext):
    await state.clear()
    prompt = message.text
    if not prompt or not prompt.strip():
        await message.answer("Пустой промпт."); return
    await _do_generate(message, message.from_user.id, prompt.strip())

@ai_router.message(AIStates.waiting_for_ref_image, F.photo)
async def process_ref_image(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    ext = ".jpg"
    if file_info.file_path:
        detected = os.path.splitext(file_info.file_path)[1].lower()
        if detected in [".jpg", ".jpeg", ".png", ".webp"]:
            ext = detected
    ref_path = os.path.join(tempfile.gettempdir(), f"ref_{message.from_user.id}{ext}")
    await bot.download_file(file_info.file_path, ref_path)
    user_ref_image[message.from_user.id] = ref_path
    await state.set_state(AIStates.waiting_for_prompt_with_ref)
    await message.answer("<b>Фото получено!</b>\nТеперь отправьте промпт.\n/cancel для отмены.", parse_mode="HTML")

@ai_router.message(AIStates.waiting_for_ref_image)
async def process_ref_image_invalid(message: types.Message):
    await message.answer("Отправьте <b>фото</b>.\n/cancel", parse_mode="HTML")

@ai_router.message(AIStates.waiting_for_prompt_with_ref)
async def process_prompt_with_ref(message: types.Message, state: FSMContext):
    await state.clear()
    prompt = message.text
    if not prompt or not prompt.strip():
        await message.answer("Пустой промпт."); return
    ref_path = user_ref_image.get(message.from_user.id)
    await _do_generate(message, message.from_user.id, prompt.strip(), ref_image_path=ref_path)

@ai_router.message(AIStates.waiting_for_chat_message)
async def process_chat_message(message: types.Message, state: FSMContext):
    text = message.text
    if not text or not text.strip():
        await message.answer("Пустое сообщение."); return
    await _do_chat(message, message.from_user.id, text.strip())

@ai_router.message(AIStates.waiting_for_image_prompt)
async def process_image_prompt(message: types.Message, state: FSMContext):
    await state.clear()
    prompt = message.text
    if not prompt or not prompt.strip():
        await message.answer("Пустой промпт."); return
    await _do_image(message, message.from_user.id, prompt.strip())

@ai_router.message(AIStates.waiting_for_tts_text)
async def process_tts_text(message: types.Message, state: FSMContext):
    await state.clear()
    text = message.text
    if not text or not text.strip():
        await message.answer("Пустой текст."); return
    await _do_tts(message, message.from_user.id, text.strip())
