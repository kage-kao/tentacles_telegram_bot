"""File Reupload Mode Router — URL -> GigaFile.nu"""
import asyncio
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import logger, is_url, format_size, ModeFilter, MODE_FILE, progress_bar, step_indicator, status_box
from gigafile_client import gigafile_client

file_router = Router()
file_router.message.filter(ModeFilter(MODE_FILE))
file_router.callback_query.filter(ModeFilter(MODE_FILE))


class FileStates(StatesGroup):
    waiting_for_url = State()


@file_router.callback_query(F.data == "file:start")
async def cb_file_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(FileStates.waiting_for_url)
    await call.message.edit_text(
        "<b>Перезалив файла на GigaFile.nu</b>\n\n"
        "Отправьте <b>прямую ссылку</b> на скачивание файла.\n"
        "Бот скачает файл и перезальёт на gigafile.nu (хранится 100 дней).\n\n"
        "Поддерживаются файлы до 300 ГБ.\n\n"
        "/cancel для отмены.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="global:main")],
        ]),
    )
    await call.answer()


@file_router.message(FileStates.waiting_for_url, F.text)
async def process_file_url(message: types.Message, state: FSMContext):
    url = message.text.strip()

    if url.startswith('/'):
        return

    if not is_url(url):
        await message.answer("Отправьте ссылку (http:// или https://)")
        return

    await state.clear()
    import time as _time
    t0 = _time.time()
    status = await message.answer(
        status_box("File Reupload", [
            f"URL: <code>{url[:60]}{'...' if len(url)>60 else ''}</code>",
            f"",
            step_indicator(1, 2, "Скачиваю файл..."),
        ]),
        parse_mode="HTML",
    )

    last_phase = ""
    last_pct = -1

    async def progress_cb(phase: str, pct: int):
        nonlocal last_phase, last_pct
        if phase == last_phase and abs(pct - last_pct) < 5:
            return
        last_phase = phase
        last_pct = pct
        step = 1 if phase == "download" else 2
        phase_text = "Скачиваю файл" if phase == "download" else "Загружаю на GigaFile"
        try:
            await status.edit_text(
                status_box("File Reupload", [
                    step_indicator(step, 2, f"{phase_text}..."),
                    f"<code>{progress_bar(pct)}</code>",
                ]),
                parse_mode="HTML",
            )
        except Exception:
            pass

    try:
        result = await gigafile_client.upload_from_url(
            url=url,
            lifetime=100,
            progress_cb=progress_cb,
        )
        elapsed = _time.time() - t0

        if result.get('success'):
            page_url = result.get('page_url', 'N/A')
            direct_url = result.get('direct_url', 'N/A')
            filename = result.get('filename', 'file')

            await status.edit_text(
                status_box("\u2714 Файл перезалит", [
                    f"Файл: <b>{filename}</b>",
                    f"Время: <b>{elapsed:.0f}с</b>",
                    f"",
                    f"<b>Страница:</b>",
                    f"{page_url}",
                    f"",
                    f"<b>Прямая ссылка:</b>",
                    f"<code>{direct_url}</code>",
                    f"",
                    f"Хранится <b>100 дней</b> на gigafile.nu",
                ]),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Ещё файл", callback_data="file:start")],
                    [InlineKeyboardButton(text="Главное меню", callback_data="global:main")],
                ]),
            )
        else:
            error = result.get('error', 'Неизвестная ошибка')
            await status.edit_text(
                status_box("\u274c Ошибка", [f"<code>{error}</code>"]),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Попробовать снова", callback_data="file:start")],
                    [InlineKeyboardButton(text="Главное меню", callback_data="global:main")],
                ]),
            )

    except Exception as e:
        elapsed = _time.time() - t0
        logger.error(f"File reupload error: {e}")
        await status.edit_text(
            status_box("\u274c Ошибка", [
                f"Время: {elapsed:.0f}с",
                f"<code>{str(e)[:400]}</code>",
            ]),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Попробовать снова", callback_data="file:start")],
                [InlineKeyboardButton(text="Главное меню", callback_data="global:main")],
            ]),
        )
