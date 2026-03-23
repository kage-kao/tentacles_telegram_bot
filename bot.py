#!/usr/bin/env python3
"""
Universal Telegram Bot - 3 Modes
/change 1 - AI Assistant (Chat, Images, Video Gen, TTS, STT)
/change 2 - Video Tools (Merge, Download, Compress)
/change 3 - File Reupload (URL -> GigaFile.nu)
"""
import asyncio
import os
import tempfile

from aiogram import F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommand,
)
from aiogram.fsm.context import FSMContext

from aiogram import Router

from config import (
    bot, dp, logger,
    MODE_AI, MODE_VIDEO, MODE_FILE, MODE_NAMES,
    get_user_mode, set_user_mode, get_settings, ADMINS,
    CHAT_MODEL_LABELS, is_url,
    status_box, step_indicator, progress_bar, format_size, format_duration, format_expires,
)
from ai_handlers import ai_router, _do_generate, _do_stt, user_ref_image, AIStates
from video_handlers import video_router, get_video_keyboard
from file_handlers import file_router, FileStates

# Fallback router — подключается ПОСЛЕДНИМ, чтобы не блокировать
# обработчики в ai_router / video_router / file_router
fallback_router = Router()


# ══════════════ MAIN MENU ══════════════

def make_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    mode = get_user_mode(user_id)
    rows = []

    if mode == MODE_AI:
        rows.append([
            InlineKeyboardButton(text="AI Чат", callback_data="ai:menu:chat"),
            InlineKeyboardButton(text="Изображения", callback_data="ai:menu:image"),
        ])
        rows.append([
            InlineKeyboardButton(text="Видео (AI)", callback_data="ai:menu:gen_text"),
            InlineKeyboardButton(text="Видео+Фото", callback_data="ai:menu:gen_ref"),
        ])
        rows.append([
            InlineKeyboardButton(text="Озвучка TTS", callback_data="ai:menu:tts"),
            InlineKeyboardButton(text="STT", callback_data="ai:menu:stt_info"),
        ])
        rows.append([
            InlineKeyboardButton(text="Настройки AI", callback_data="ai:settings_hub"),
            InlineKeyboardButton(text="Ключи", callback_data="ai:menu:keys"),
        ])
    elif mode == MODE_VIDEO:
        rows.append([InlineKeyboardButton(text="Склеить видео", callback_data="vid:merge")])
        rows.append([InlineKeyboardButton(text="Скачать видео", callback_data="vid:download")])
        rows.append([InlineKeyboardButton(text="Сжать видео", callback_data="vid:compress")])
    elif mode == MODE_FILE:
        rows.append([InlineKeyboardButton(text="Перезалить файл", callback_data="file:start")])

    rows.append([InlineKeyboardButton(text="Сменить режим", callback_data="global:change")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def make_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. AI Ассистент", callback_data="mode:1")],
        [InlineKeyboardButton(text="2. Видео инструменты", callback_data="mode:2")],
        [InlineKeyboardButton(text="3. Перезалив файлов", callback_data="mode:3")],
    ])


# ══════════════ /start ══════════════

async def set_bot_commands():
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="change", description="Сменить режим бота"),
        BotCommand(command="chat", description="AI Чат"),
        BotCommand(command="image", description="Генерация изображений"),
        BotCommand(command="gen", description="Генерация видео (AI)"),
        BotCommand(command="genref", description="Видео с референс фото"),
        BotCommand(command="tts", description="Озвучка текста"),
        BotCommand(command="key", description="Universal Keys"),
        BotCommand(command="merge", description="Склейка видео"),
        BotCommand(command="download", description="Скачать видео по ссылке"),
        BotCommand(command="compress", description="Сжать видео"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="cancel", description="Отмена"),
    ]
    await bot.set_my_commands(commands)


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id

    if not ADMINS:
        ADMINS.add(uid)
        logger.info(f"Admin: {uid}")

    mode = get_user_mode(uid)
    mode_name = MODE_NAMES.get(mode, "AI")

    mode_icons = {MODE_AI: "\u2728", MODE_VIDEO: "\u25b6", MODE_FILE: "\u2601"}
    icon = mode_icons.get(mode, "")

    border = "\u2500" * 24
    await message.answer(
        f"<b>{icon} Universal Bot</b>\n"
        f"{border}\n"
        f"Режим: <b>{mode_name}</b>\n"
        f"{border}\n\n"
        f"<b>/change</b> \u2014 сменить режим\n"
        f"<b>/help</b> \u2014 справка\n"
        f"<b>/cancel</b> \u2014 отмена",
        parse_mode="HTML",
        reply_markup=make_main_keyboard(uid),
    )


@dp.callback_query(F.data == "global:main")
async def cb_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    mode_name = MODE_NAMES.get(get_user_mode(uid), "AI")
    await call.message.edit_text(
        f"<b>Universal Bot</b>\n\nРежим: <b>{mode_name}</b>",
        parse_mode="HTML",
        reply_markup=make_main_keyboard(uid),
    )
    await call.answer()


# ══════════════ /change ══════════════

@dp.message(Command("change"))
async def cmd_change(message: types.Message, state: FSMContext):
    args = message.text.split()
    if len(args) >= 2:
        try:
            mode = int(args[1])
            if mode in (1, 2, 3):
                await state.clear()
                set_user_mode(message.from_user.id, mode)
                mode_name = MODE_NAMES.get(mode, "?")
                await message.answer(
                    f"Режим: <b>{mode_name}</b>",
                    parse_mode="HTML",
                    reply_markup=make_main_keyboard(message.from_user.id),
                )
                return
        except ValueError:
            pass

    current = get_user_mode(message.from_user.id)
    await message.answer(
        f"<b>Сменить режим</b>\n\n"
        f"Текущий: <b>{MODE_NAMES.get(current, '?')}</b>\n\n"
        f"<b>/change 1</b> — AI Ассистент\n"
        f"<b>/change 2</b> — Видео инструменты\n"
        f"<b>/change 3</b> — Перезалив файлов",
        parse_mode="HTML",
        reply_markup=make_mode_keyboard(),
    )


@dp.callback_query(F.data == "global:change")
async def cb_change(call: CallbackQuery):
    current = get_user_mode(call.from_user.id)
    await call.message.edit_text(
        f"<b>Сменить режим</b>\n\nТекущий: <b>{MODE_NAMES.get(current, '?')}</b>",
        parse_mode="HTML",
        reply_markup=make_mode_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("mode:"))
async def cb_mode_select(call: CallbackQuery, state: FSMContext):
    try:
        mode = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Ошибка")
        return

    if mode not in (1, 2, 3):
        await call.answer("Неверный режим")
        return

    await state.clear()
    set_user_mode(call.from_user.id, mode)
    mode_name = MODE_NAMES.get(mode, "?")

    await call.message.edit_text(
        f"Режим: <b>{mode_name}</b>",
        parse_mode="HTML",
        reply_markup=make_main_keyboard(call.from_user.id),
    )
    await call.answer(f"Режим: {mode_name}")


# ══════════════ /help ══════════════

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    mode = get_user_mode(message.from_user.id)

    if mode == MODE_AI:
        text = (
            "<b>AI Ассистент — Справка</b>\n\n"
            "/chat — AI чат (GPT-4o, Claude, Gemini)\n"
            "/image — Генерация изображений\n"
            "/gen — Генерация видео (Sora 2)\n"
            "/genref — Видео с референс фото\n"
            "/tts — Озвучка текста\n"
            "Голосовое — распознавание речи\n"
            "/key — Управление ключами\n"
            "/settings — Настройки AI"
        )
    elif mode == MODE_VIDEO:
        text = (
            "<b>Видео инструменты — Справка</b>\n\n"
            "<b>Склейка:</b>\n"
            "/merge — Начать склейку\n"
            "/merge_now — Склеить\n"
            "/watermark — Водяной знак\n"
            "/size N — Размер знака (1-50%)\n"
            "/speed N — Скорость (10-200)\n\n"
            "<b>Скачивание:</b>\n"
            "/download — 1000+ сайтов\n\n"
            "<b>Сжатие:</b>\n"
            "/compress — FFmpeg или ezgif\n"
            "<code>/compress URL размер [h264|h265|av1]</code>"
        )
    elif mode == MODE_FILE:
        text = (
            "<b>Перезалив файлов — Справка</b>\n\n"
            "Отправьте прямую ссылку на файл.\n"
            "Бот скачает и перезальёт на gigafile.nu.\n\n"
            "Хранится 100 дней.\n"
            "Файлы до 300 ГБ."
        )
    else:
        text = "<b>Справка</b>\n\n/change — сменить режим"

    text += "\n\n<b>Общие:</b>\n/change — сменить режим\n/cancel — отмена\n/start — главное меню"

    await message.answer(text, parse_mode="HTML", reply_markup=make_main_keyboard(message.from_user.id))


@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    mode = get_user_mode(message.from_user.id)
    if mode == MODE_AI:
        from ai_handlers import make_settings_hub_keyboard
        await message.answer("<b>Настройки AI</b>\n\nВыберите раздел:", parse_mode="HTML", reply_markup=make_settings_hub_keyboard())
    else:
        await message.answer("Настройки доступны в режиме AI.\n/change 1", reply_markup=make_main_keyboard(message.from_user.id))


# ══════════════ /cancel ══════════════

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять.", reply_markup=make_main_keyboard(message.from_user.id))
        return
    # Cancel video compress tasks
    from video_handlers import active_compress_tasks, active_compress_jobs
    uid = message.from_user.id
    task = active_compress_tasks.pop(uid, None)
    if task and not task.done():
        task.cancel()
    active_compress_jobs.pop(uid, None)
    await state.clear()
    await message.answer("Отменено.", reply_markup=make_main_keyboard(message.from_user.id))


# ══════════════ WRONG MODE COMMAND CATCHER (fallback_router) ══════════════
# Эти обработчики в fallback_router срабатывают ТОЛЬКО если ни один
# mode-specific роутер (ai/video/file) не обработал команду.

AI_COMMANDS = {"chat", "image", "gen", "genref", "tts", "key", "admin"}
VIDEO_COMMANDS = {"merge", "merge_now", "merge_clear", "merge_status", "watermark", "size", "speed", "download", "compress", "vstatus"}

@fallback_router.message(Command(*AI_COMMANDS))
async def wrong_mode_ai(message: types.Message):
    """Пользователь вызвал AI-команду не в AI-режиме."""
    await message.answer(
        "Эта команда работает только в режиме <b>AI Ассистент</b>.\n"
        "Переключитесь: /change 1",
        parse_mode="HTML",
        reply_markup=make_mode_keyboard(),
    )

@fallback_router.message(Command(*VIDEO_COMMANDS))
async def wrong_mode_video(message: types.Message):
    """Пользователь вызвал видео-команду не в видео-режиме."""
    await message.answer(
        "Эта команда работает только в режиме <b>Видео инструменты</b>.\n"
        "Переключитесь: /change 2",
        parse_mode="HTML",
        reply_markup=make_mode_keyboard(),
    )


# ══════════════ FALLBACK HANDLERS (mode-aware, fallback_router) ══════════════
# Эти обработчики срабатывают ТОЛЬКО если ни один роутер не обработал
# сообщение. Это гарантирует, что FSM-обработчики в роутерах работают.

@fallback_router.message(F.voice)
async def handle_voice(message: types.Message, state: FSMContext):
    mode = get_user_mode(message.from_user.id)
    if mode != MODE_AI:
        return
    current = await state.get_state()
    if current and current != AIStates.waiting_for_chat_message.state:
        return
    file_info = await bot.get_file(message.voice.file_id)
    tmp_path = os.path.join(tempfile.gettempdir(), f"voice_{message.from_user.id}.mp3")
    await bot.download_file(file_info.file_path, tmp_path)
    await _do_stt(message, message.from_user.id, tmp_path)


@fallback_router.message(F.audio)
async def handle_audio(message: types.Message, state: FSMContext):
    mode = get_user_mode(message.from_user.id)
    if mode != MODE_AI:
        return
    current = await state.get_state()
    if current and current != AIStates.waiting_for_chat_message.state:
        return
    file_info = await bot.get_file(message.audio.file_id)
    ext = ".mp3"
    if file_info.file_path:
        detected = os.path.splitext(file_info.file_path)[1].lower()
        if detected in [".mp3", ".mp4", ".mpeg", ".wav", ".webm", ".m4a"]:
            ext = detected
    tmp_path = os.path.join(tempfile.gettempdir(), f"audio_{message.from_user.id}{ext}")
    await bot.download_file(file_info.file_path, tmp_path)
    await _do_stt(message, message.from_user.id, tmp_path)


@fallback_router.message(F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    current = await state.get_state()
    if current:
        return

    mode = get_user_mode(message.from_user.id)
    if mode != MODE_AI:
        return

    # AI mode: save as ref image for video gen
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

    caption = message.caption
    if caption and caption.strip() and not caption.startswith('/'):
        await _do_generate(message, message.from_user.id, caption.strip(), ref_image_path=ref_path)
    else:
        await state.set_state(AIStates.waiting_for_prompt_with_ref)
        await message.answer(
            "<b>Фото сохранено!</b>\nОтправьте промпт для видео.\n/cancel для отмены.",
            parse_mode="HTML",
        )


@fallback_router.message(F.text)
async def handle_text(message: types.Message, state: FSMContext):
    current = await state.get_state()
    if current:
        return

    text = message.text.strip()
    if not text or text.startswith('/'):
        return

    mode = get_user_mode(message.from_user.id)

    if mode == MODE_AI:
        # Default: generate video from text
        await _do_generate(message, message.from_user.id, text)

    elif mode == MODE_VIDEO:
        # Default: auto-download if URL
        if is_url(text):
            from video_handlers import download_video_ytdlp, upload_to_tempshare
            import tempfile as tf
            import shutil
            import time as _time
            t0 = _time.time()
            status = await message.answer(
                status_box("Download Video", [
                    f"URL: <code>{text[:60]}...</code>",
                    f"",
                    step_indicator(1, 3, "Скачиваю..."),
                ]),
                parse_mode="HTML",
            )
            tmp = tf.mkdtemp(prefix="dl_")
            try:
                fp = await download_video_ytdlp(text, tmp)
                if not fp or not os.path.exists(fp):
                    await status.edit_text(
                        status_box("\u274c Ошибка", ["Не удалось скачать видео."]),
                        parse_mode="HTML",
                    )
                    return
                mb = os.path.getsize(fp) / (1024*1024)
                elapsed = _time.time() - t0
                await status.edit_text(
                    status_box("Download Video", [
                        f"Скачано: <b>{mb:.1f} МБ</b> за {elapsed:.0f}с",
                        f"",
                        step_indicator(2, 3, "Загрузка на TempShare..."),
                    ]),
                    parse_mode="HTML",
                )
                r = await upload_to_tempshare(fp)
                elapsed = _time.time() - t0
                if r and r.get("success"):
                    await status.edit_text(
                        status_box("\u2714 Видео скачано", [
                            f"Размер: <b>{mb:.1f} МБ</b> | Время: <b>{format_duration(elapsed)}</b>",
                            f"",
                            f"<b>Ссылка:</b>",
                            f"<code>{r.get('raw_url','')}</code>",
                            f"",
                            f"До: {format_expires(r.get('expires',''))}",
                        ]),
                        parse_mode="HTML",
                    )
                else:
                    await status.edit_text(
                        status_box("\u274c Ошибка загрузки", ["TempShare недоступен."]),
                        parse_mode="HTML",
                    )
            except Exception as e:
                await status.edit_text(
                    status_box("\u274c Ошибка", [f"<code>{str(e)[:300]}</code>"]),
                    parse_mode="HTML",
                )
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        else:
            await message.answer(
                "Отправьте ссылку или используйте команды:\n/merge /download /compress",
                reply_markup=get_video_keyboard())

    elif mode == MODE_FILE:
        # Default: reupload URL to gigafile
        if is_url(text):
            from gigafile_client import gigafile_client
            import time as _time
            t0 = _time.time()

            status = await message.answer(
                status_box("File Reupload", [
                    f"URL: <code>{text[:60]}...</code>",
                    f"",
                    step_indicator(1, 2, "Скачиваю файл..."),
                ]),
                parse_mode="HTML",
            )
            last_phase = ""
            last_pct = -1

            async def progress_cb(phase, pct):
                nonlocal last_phase, last_pct
                if phase == last_phase and abs(pct - last_pct) < 5:
                    return
                last_phase = phase
                last_pct = pct
                step = 1 if phase == "download" else 2
                pt = "Скачиваю файл" if phase == "download" else "Загружаю на GigaFile"
                try:
                    await status.edit_text(
                        status_box("File Reupload", [
                            step_indicator(step, 2, f"{pt}..."),
                            f"<code>{progress_bar(pct)}</code>",
                        ]),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            try:
                result = await gigafile_client.upload_from_url(url=text, lifetime=100, progress_cb=progress_cb)
                elapsed = _time.time() - t0
                if result.get('success'):
                    pu = result.get('page_url', 'N/A')
                    du = result.get('direct_url', 'N/A')
                    fn = result.get('filename', 'file')
                    await status.edit_text(
                        status_box("\u2714 Файл перезалит", [
                            f"Файл: <b>{fn}</b>",
                            f"Время: <b>{elapsed:.0f}с</b>",
                            f"",
                            f"<b>Страница:</b>",
                            f"{pu}",
                            f"<b>Прямая:</b>",
                            f"<code>{du}</code>",
                            f"",
                            f"Хранится <b>100 дней</b>",
                        ]),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="Ещё файл", callback_data="file:start")],
                            [InlineKeyboardButton(text="Меню", callback_data="global:main")],
                        ]))
                else:
                    await status.edit_text(
                        status_box("\u274c Ошибка", [result.get('error', '?')]),
                        parse_mode="HTML",
                    )
            except Exception as e:
                logger.error(f"File reupload error: {e}")
                await status.edit_text(
                    status_box("\u274c Ошибка", [f"<code>{str(e)[:400]}</code>"]),
                    parse_mode="HTML",
                )
        else:
            await message.answer("Отправьте прямую ссылку на файл для перезалива на GigaFile.nu")


# ══════════════ INCLUDE ROUTERS ══════════════

# Порядок ВАЖЕН: mode-specific роутеры ПЕРВЫМИ,
# fallback_router ПОСЛЕДНИМ (wrong-mode + catch-all обработчики)
dp.include_router(ai_router)
dp.include_router(video_router)
dp.include_router(file_router)
dp.include_router(fallback_router)


# ══════════════ MAIN ══════════════

async def main():
    logger.info("Starting Universal Bot...")
    await set_bot_commands()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped (KeyboardInterrupt)")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        raise
