"""Video Mode Router — Merge, Download, Compress (FFmpeg + ezgif)"""
import asyncio
import json
import math
import os
import tempfile
import uuid
import shutil
import time
from pathlib import Path
from typing import Dict, List
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor

import aiofiles
import aiohttp
import httpx
import yt_dlp
import requests
from bs4 import BeautifulSoup
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode

from config import bot, logger, format_size, format_duration, format_expires, is_url, ModeFilter, MODE_VIDEO, progress_bar, step_indicator, status_box
from compressor import compress_video, get_video_info

video_router = Router()
video_router.message.filter(ModeFilter(MODE_VIDEO))
video_router.callback_query.filter(ModeFilter(MODE_VIDEO))

FFMPEG_SEMAPHORE = asyncio.Semaphore(3)
FFMPEG_THREADS = str(os.cpu_count() or 4)
MAX_CONCURRENT_JOBS = 1
MAX_DOWNLOAD_SIZE_MB = 1024
DOWNLOAD_CHUNK_SIZE = 256 * 1024

_job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
active_compress_jobs: Dict[int, dict] = {}
active_compress_tasks: Dict[int, asyncio.Task] = {}

TEMPSHARE_API = "https://api.tempshare.su/upload"
EZGIF_BASE = "https://ezgif.com"
EZGIF_COMPRESS_URL = f"{EZGIF_BASE}/video-compressor"
EZGIF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": EZGIF_COMPRESS_URL,
}
RESOLUTIONS = {"original": "Оригинал", "1920x1080": "Full HD", "1280x720": "HD", "854x480": "SD", "640x360": "360p", "426x240": "240p"}
BITRATES = {"1000": "1000 kbps", "750": "750 kbps", "500": "500 kbps", "300": "300 kbps", "150": "150 kbps"}
EZGIF_FORMATS = {"mp4": "MP4", "webm": "WebM", "mkv": "MKV"}
EZGIF_CHUNK_MB = 190

ezgif_executor = ThreadPoolExecutor(max_workers=4)
ezgif_pending: Dict[int, dict] = {}

# ── States ──
class MergeStates(StatesGroup):
    collecting_videos = State()
    waiting_for_watermark = State()

class DownloadStates(StatesGroup):
    waiting_for_url = State()

class CompressStates(StatesGroup):
    waiting_for_url = State()
    waiting_for_size = State()
    waiting_for_custom_size = State()
    waiting_for_codec = State()

class EzgifStates(StatesGroup):
    waiting_for_url = State()
    waiting_for_resolution = State()
    waiting_for_bitrate = State()
    waiting_for_format = State()

# ── Merge data ──
@dataclass
class MergeUserData:
    videos: List[str] = field(default_factory=list)
    watermark_path: str | None = None
    watermark_size: int = 15
    watermark_speed: float = 50.0
    temp_dir: str | None = None

merge_sessions: Dict[int, MergeUserData] = {}

def get_merge_data(uid):
    if uid not in merge_sessions:
        merge_sessions[uid] = MergeUserData(temp_dir=tempfile.mkdtemp(prefix=f"merge_{uid}_"))
    return merge_sessions[uid]

def cleanup_merge_data(uid):
    if uid in merge_sessions:
        d = merge_sessions[uid]
        if d.temp_dir and os.path.exists(d.temp_dir):
            shutil.rmtree(d.temp_dir, ignore_errors=True)
        del merge_sessions[uid]

def clear_merge_videos(uid):
    if uid not in merge_sessions: return
    d = merge_sessions[uid]
    for v in d.videos:
        try: os.remove(v)
        except OSError: pass
    d.videos = []

def _fmt_time(s):
    m = int(s // 60); s = int(s % 60)
    return f"{m} мин {s} сек" if m > 0 else f"{s} сек"

# ── FFmpeg helpers ──
async def run_ffmpeg(cmd, desc="ffmpeg"):
    async with FFMPEG_SEMAPHORE:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"{desc} failed: {stderr.decode(errors='replace')[-800:]}")
        return stdout, stderr

async def probe_video(path):
    proc = await asyncio.create_subprocess_exec('ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,duration', '-show_entries', 'format=duration', '-of', 'csv=p=0', path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await proc.communicate()
    lines = stdout.decode().strip().split('\n')
    w, h, dur = 1280, 720, 60.0
    try:
        parts = lines[0].strip().split(',')
        w = int(parts[0]); h = int(parts[1])
        if len(parts) > 2 and parts[2] != 'N/A': dur = float(parts[2])
        elif len(lines) > 1 and lines[1].strip() != 'N/A': dur = float(lines[1].strip())
    except (ValueError, IndexError): pass
    return {'width': w, 'height': h, 'duration': dur}

async def probe_image(path):
    proc = await asyncio.create_subprocess_exec('ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await proc.communicate()
    w, h = 100, 100
    try:
        parts = stdout.decode().strip().split(',')
        w = int(parts[0]); h = int(parts[1])
    except (ValueError, IndexError): pass
    return {'width': w, 'height': h}

async def normalize_single_video(i, path, temp_dir):
    out = os.path.join(temp_dir, f"norm_{i}.mp4")
    cmd = ['ffmpeg', '-y', '-threads', FFMPEG_THREADS, '-i', path, '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-ar', '44100', '-ac', '2', '-vf', 'scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1', '-r', '30', '-max_muxing_queue_size', '2048', '-movflags', '+faststart', out]
    await run_ffmpeg(cmd, f"normalize {i+1}")
    return out

async def merge_videos_process(data, progress_cb=None):
    if len(data.videos) < 1: raise ValueError("Need at least 1 video")
    out = os.path.join(data.temp_dir, f"merged_{uuid.uuid4().hex[:8]}.mp4")
    lf = os.path.join(data.temp_dir, "videos.txt")
    if progress_cb: await progress_cb(f"Processing {len(data.videos)} videos...")
    normalized = await asyncio.gather(*[normalize_single_video(i, v, data.temp_dir) for i, v in enumerate(data.videos)])
    async with aiofiles.open(lf, 'w') as f:
        for v in normalized: await f.write(f"file '{v}'\n")
    if progress_cb: await progress_cb("Concatenating...")
    concat_out = os.path.join(data.temp_dir, "concat.mp4")
    await run_ffmpeg(['ffmpeg', '-y', '-threads', FFMPEG_THREADS, '-f', 'concat', '-safe', '0', '-i', lf, '-c', 'copy', '-movflags', '+faststart', concat_out], "concat")
    if data.watermark_path and os.path.exists(data.watermark_path):
        if progress_cb: await progress_cb("Adding watermark...")
        vi = await probe_video(concat_out); wi = await probe_image(data.watermark_path)
        wm_h = max(2, (int(vi['height'] * data.watermark_size / 100) // 2) * 2)
        wm_w = max(2, (int(wi['width'] * wm_h / max(wi['height'], 1)) // 2) * 2)
        max_w = max(2, ((vi['width'] - 4) // 2) * 2)
        if wm_w > max_w: wm_w = max_w; wm_h = max(2, (int(wi['height'] * wm_w / max(wi['width'], 1)) // 2) * 2)
        rx = max(1, vi['width'] - wm_w); ry = max(1, vi['height'] - wm_h)
        sp = data.watermark_speed; spy = sp * 0.7
        flt = f"[1:v]scale={wm_w}:{wm_h},format=rgba[wm];[0:v][wm]overlay=x='abs(mod(t*{sp},{rx*2})-{rx})':y='abs(mod(t*{spy},{ry*2})-{ry})'"
        await run_ffmpeg(['ffmpeg', '-y', '-threads', FFMPEG_THREADS, '-i', concat_out, '-i', data.watermark_path, '-filter_complex', flt, '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'copy', '-max_muxing_queue_size', '2048', '-movflags', '+faststart', out], "watermark")
    else:
        shutil.copy(concat_out, out)
    return out

async def is_valid_video(path):
    proc = await asyncio.create_subprocess_exec('ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await proc.communicate()
    return proc.returncode == 0 and b'video' in stdout

# ── Upload helpers ──
async def upload_to_tempshare(path, duration=3):
    timeout = aiohttp.ClientTimeout(total=600)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with open(path, 'rb') as f:
            form = aiohttp.FormData()
            form.add_field('file', f, filename=os.path.basename(path))
            form.add_field('duration', str(duration))
            async with session.post(TEMPSHARE_API, data=form) as resp:
                return await resp.json() if resp.status == 200 else {"success": False}

async def download_video_ytdlp(url, download_dir):
    opts = {"format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best", "outtmpl": os.path.join(download_dir, "%(title).80s.%(ext)s"), "merge_output_format": "mp4", "quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30, "retries": 3}
    loop = asyncio.get_running_loop()
    def _dl():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info: return None
            fn = ydl.prepare_filename(info)
            b, _ = os.path.splitext(fn); mp4 = b + ".mp4"
            if os.path.exists(mp4): return mp4
            if os.path.exists(fn): return fn
            for f in os.listdir(download_dir):
                fp = os.path.join(download_dir, f)
                if os.path.isfile(fp): return fp
            return None
    try: return await loop.run_in_executor(None, _dl)
    except Exception as e: logger.error(f"Download error: {e}"); return None

# ── ezgif sync functions ──
def ezgif_step1_upload(url):
    s = requests.Session(); s.headers.update(EZGIF_HEADERS)
    resp = s.post(EZGIF_COMPRESS_URL, data={"new-image-url": url, "upload": "Upload video!"}, timeout=120); resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    fi = soup.find("input", {"name": "file"})
    if not fi:
        err = soup.find("p", class_="error")
        raise Exception(f"ezgif: {err.get_text(strip=True)}" if err else "ezgif: file ID not found")
    fid = fi["value"]; form = soup.find("form", class_="ajax-form")
    action = form["action"] if form and form.get("action") else f"{EZGIF_COMPRESS_URL}/{fid}"
    return {"file_id": fid, "action_url": action, "session": s}

def ezgif_step2_compress(fid, action, session, res="original", br=500, fmt="mp4"):
    resp = session.post(action, data={"file": fid, "resolution": res, "bitrate": str(br), "format": fmt, "video-compressor": "Recompress video!"}, timeout=300); resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    od = soup.find("div", id="output")
    if not od:
        err = soup.find("p", class_="error")
        raise Exception(f"ezgif: {err.get_text(strip=True)}" if err else "Compression failed")
    vs = None; vt = od.find("video")
    if vt:
        src = vt.find("source")
        if src and src.get("src"):
            vs = src["src"]
            if vs.startswith("//"): vs = "https:" + vs
            elif vs.startswith("/"): vs = EZGIF_BASE + vs
    sl = od.find("a", class_="save") or (soup.find_all("a", class_="save") or [None])[-1]
    if sl and sl.get("href"):
        su = sl["href"]
        if su.startswith("/"): su = EZGIF_BASE + su
    elif vs: su = vs
    else: raise Exception("No compressed video URL found")
    fs = od.find("p", class_="filestats")
    fi = fs.get_text(" ", strip=True)[:150] if fs else "Unknown"
    return {"save_url": su, "file_info": fi, "session": session}

def ezgif_step3_download(save_url, session):
    resp = session.get(save_url, timeout=300, stream=True); resp.raise_for_status()
    ext = ".mp4"
    for f in [".webm", ".mkv"]:
        if f[1:] in save_url: ext = f; break
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    for chunk in resp.iter_content(chunk_size=8192): tmp.write(chunk)
    tmp.close(); return tmp.name

def ezgif_step4_upload_tempshare(path, dur=7):
    with open(path, "rb") as f:
        resp = requests.post(TEMPSHARE_API, files={"file": (os.path.basename(path), f)}, data={"duration": str(dur)}, timeout=300)
    resp.raise_for_status(); r = resp.json()
    if not r.get("success"): raise Exception(f"tempshare failed: {r}")
    return r

# ── Video mode keyboard ──
def get_video_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Склеить видео", callback_data="vid:merge")],
        [InlineKeyboardButton(text="Скачать видео", callback_data="vid:download")],
        [InlineKeyboardButton(text="Сжать видео", callback_data="vid:compress")],
        [InlineKeyboardButton(text="Назад", callback_data="global:main")],
    ])

# ══════════════ MERGE HANDLERS ══════════════

@video_router.callback_query(F.data == "vid:merge")
async def cb_merge(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id; get_merge_data(uid)
    await state.set_state(MergeStates.collecting_videos)
    await call.message.edit_text(
        "<b>Склейка видео</b>\n\nОтправляйте видео файлы.\n\n"
        "/merge_now — Склеить\n/watermark — Водяной знак\n/size N — Размер знака (1-50%)\n/speed N — Скорость (10-200)\n/merge_clear — Очистить\n/merge_status — Статус",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="global:main")]]))
    await call.answer()

@video_router.message(Command("merge"))
async def cmd_merge(msg: types.Message, state: FSMContext):
    get_merge_data(msg.from_user.id)
    await state.set_state(MergeStates.collecting_videos)
    await msg.answer("<b>Склейка видео</b>\n\nОтправляйте видео.\n/merge_now — склеить\n/watermark — водяной знак\n/merge_clear — очистить", parse_mode=ParseMode.HTML)

@video_router.message(Command("merge_clear"))
async def cmd_merge_clear(msg: types.Message):
    d = get_merge_data(msg.from_user.id); had_wm = d.watermark_path is not None; clear_merge_videos(msg.from_user.id)
    await msg.answer(f"Все видео удалены.{' Водяной знак сохранён.' if had_wm else ''}")

@video_router.message(Command("merge_status"))
async def cmd_merge_status(msg: types.Message):
    d = get_merge_data(msg.from_user.id)
    await msg.answer(f"<b>Статус склейки:</b>\nВидео: <b>{len(d.videos)}</b>\nВодяной знак: {'Да' if d.watermark_path else 'Нет'}\nРазмер: <b>{d.watermark_size}%</b> | Скорость: <b>{d.watermark_speed}</b>", parse_mode=ParseMode.HTML)

@video_router.message(Command("watermark"))
async def cmd_watermark(msg: types.Message, state: FSMContext):
    await state.set_state(MergeStates.waiting_for_watermark)
    await msg.answer("Отправьте изображение для водяного знака.\n/cancel — отмена")

@video_router.message(Command("size"))
async def cmd_size(msg: types.Message):
    d = get_merge_data(msg.from_user.id)
    try:
        parts = msg.text.split()
        if len(parts) < 2: await msg.answer("Пример: <code>/size 20</code>", parse_mode=ParseMode.HTML); return
        sz = int(parts[1])
        if not 1 <= sz <= 50: await msg.answer("От 1 до 50%"); return
        d.watermark_size = sz
        await msg.answer(f"Размер водяного знака: <b>{sz}%</b>", parse_mode=ParseMode.HTML)
    except ValueError: await msg.answer("Введите число. <code>/size 15</code>", parse_mode=ParseMode.HTML)

@video_router.message(Command("speed"))
async def cmd_speed(msg: types.Message):
    d = get_merge_data(msg.from_user.id)
    try:
        parts = msg.text.split()
        if len(parts) < 2: await msg.answer("Пример: <code>/speed 80</code>", parse_mode=ParseMode.HTML); return
        sp = float(parts[1])
        if not 10 <= sp <= 200: await msg.answer("От 10 до 200"); return
        d.watermark_speed = sp
        await msg.answer(f"Скорость: <b>{sp}</b> px/сек", parse_mode=ParseMode.HTML)
    except ValueError: await msg.answer("Введите число. <code>/speed 50</code>", parse_mode=ParseMode.HTML)

@video_router.message(Command("merge_now"))
async def cmd_merge_now(msg: types.Message):
    uid = msg.from_user.id; d = get_merge_data(uid)
    if len(d.videos) < 1: await msg.answer("Нет видео! Отправьте минимум 1."); return
    if d.watermark_path and not os.path.exists(d.watermark_path): d.watermark_path = None
    import time as _time
    t0 = _time.time()
    total_steps = 3 if d.watermark_path else 2
    status = await msg.answer(
        status_box("Video Merge", [
            f"Видео: <b>{len(d.videos)}</b>",
            f"Водяной знак: {'Да' if d.watermark_path else 'Нет'}",
            f"",
            step_indicator(1, total_steps, "Нормализация..."),
        ]),
        parse_mode=ParseMode.HTML,
    )
    async def upd(t):
        elapsed = _time.time() - t0
        try:
            await status.edit_text(
                status_box("Video Merge", [
                    f"Видео: <b>{len(d.videos)}</b> | Время: {elapsed:.0f}с",
                    f"",
                    f"\u23f3 {t}",
                ]),
                parse_mode=ParseMode.HTML,
            )
        except Exception: pass
    try:
        merged = await merge_videos_process(d, upd)
        sz = os.path.getsize(merged); mb = sz / (1024*1024)
        elapsed = _time.time() - t0
        await status.edit_text(
            status_box("Video Merge", [
                f"Склеено: <b>{mb:.1f} МБ</b> за {elapsed:.0f}с",
                f"",
                step_indicator(total_steps, total_steps, "Загрузка на TempShare..."),
            ]),
            parse_mode=ParseMode.HTML,
        )
        r = await upload_to_tempshare(merged, 3)
        elapsed = _time.time() - t0
        if r.get('success'):
            await status.edit_text(
                status_box("\u2714 Видео склеено", [
                    f"Видео: <b>{len(d.videos)}</b> | Размер: <b>{mb:.1f} МБ</b>",
                    f"Время: <b>{format_duration(elapsed)}</b>",
                    f"",
                    f"<b>Ссылка:</b>",
                    f"{r.get('url','')}",
                    f"<b>Прямая:</b>",
                    f"<code>{r.get('raw_url','')}</code>",
                    f"",
                    f"До: {format_expires(r.get('expires',''))}",
                ]),
                parse_mode=ParseMode.HTML,
            )
        else:
            await status.edit_text(
                status_box("\u274c Ошибка загрузки", [f"{r}"]),
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.exception("Merge error")
        await status.edit_text(
            status_box("\u274c Ошибка склейки", [f"<code>{str(e)[:300]}</code>"]),
            parse_mode=ParseMode.HTML,
        )

@video_router.message(MergeStates.waiting_for_watermark, F.document)
async def wm_doc(msg: types.Message, state: FSMContext):
    d = get_merge_data(msg.from_user.id); doc = msg.document
    if not doc.mime_type or not doc.mime_type.startswith('image/'): await msg.answer("Отправьте изображение!"); return
    f = await bot.get_file(doc.file_id); ext = Path(doc.file_name).suffix if doc.file_name else '.png'
    wp = os.path.join(d.temp_dir, f"watermark{ext}"); await bot.download_file(f.file_path, wp)
    d.watermark_path = wp; await state.set_state(MergeStates.collecting_videos)
    await msg.answer(f"Водяной знак установлен! Размер: {d.watermark_size}%")

@video_router.message(MergeStates.waiting_for_watermark, F.photo)
async def wm_photo(msg: types.Message, state: FSMContext):
    d = get_merge_data(msg.from_user.id); photo = msg.photo[-1]
    f = await bot.get_file(photo.file_id); wp = os.path.join(d.temp_dir, "watermark.jpg")
    await bot.download_file(f.file_path, wp); d.watermark_path = wp
    await state.set_state(MergeStates.collecting_videos)
    await msg.answer(f"Водяной знак установлен! Размер: {d.watermark_size}%")

# ══════════════ DOWNLOAD HANDLERS ══════════════

@video_router.callback_query(F.data == "vid:download")
async def cb_download(call: CallbackQuery, state: FSMContext):
    await state.set_state(DownloadStates.waiting_for_url)
    await call.message.edit_text(
        "<b>Скачивание видео</b>\n\nОтправьте ссылку на видео (1000+ сайтов).\n/cancel — отмена",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="global:main")]]))
    await call.answer()

@video_router.message(Command("download"))
async def cmd_download(msg: types.Message, state: FSMContext):
    await state.set_state(DownloadStates.waiting_for_url)
    await msg.answer("<b>Скачивание видео</b>\n\nОтправьте ссылку.\n/cancel — отмена", parse_mode=ParseMode.HTML)

@video_router.message(DownloadStates.waiting_for_url, F.text)
async def dl_url(msg: types.Message, state: FSMContext):
    url = msg.text.strip()
    if url.startswith('/'): return
    if not is_url(url): await msg.answer("Отправьте ссылку (http:// или https://)"); return
    await state.clear()
    import time as _time
    t0 = _time.time()
    status = await msg.answer(
        status_box("Download Video", [
            f"URL: <code>{url[:60]}{'...' if len(url)>60 else ''}</code>",
            f"",
            step_indicator(1, 3, "Скачиваю видео..."),
        ]),
        parse_mode=ParseMode.HTML,
    )
    tmp = tempfile.mkdtemp(prefix="dl_")
    try:
        fp = await download_video_ytdlp(url, tmp)
        if not fp or not os.path.exists(fp):
            await status.edit_text(
                status_box("\u274c Ошибка скачивания", [
                    "Не удалось скачать видео.",
                    "Проверьте ссылку.",
                ]),
                parse_mode=ParseMode.HTML,
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
            parse_mode=ParseMode.HTML,
        )
        r = await upload_to_tempshare(fp)
        elapsed = _time.time() - t0
        if r and r.get("success"):
            await status.edit_text(
                status_box("\u2714 Видео скачано", [
                    f"Размер: <b>{mb:.1f} МБ</b>",
                    f"Время: <b>{format_duration(elapsed)}</b>",
                    f"",
                    f"<b>Ссылка:</b>",
                    f"<code>{r.get('raw_url','')}</code>",
                    f"",
                    f"До: {format_expires(r.get('expires',''))}",
                ]),
                parse_mode=ParseMode.HTML,
            )
        else:
            await status.edit_text(
                status_box("\u274c Ошибка загрузки", ["Не удалось загрузить на TempShare."]),
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.error(f"DL error: {e}")
        await status.edit_text(
            status_box("\u274c Ошибка", [f"<code>{str(e)[:300]}</code>"]),
            parse_mode=ParseMode.HTML,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ══════════════ COMPRESS MENU ══════════════

@video_router.callback_query(F.data == "vid:compress")
async def cb_compress(call: CallbackQuery, state: FSMContext):
    kb = [
        [InlineKeyboardButton(text="FFmpeg (локальное)", callback_data="vid:cv1")],
        [InlineKeyboardButton(text="ezgif.com (облачное)", callback_data="vid:cv2")],
        [InlineKeyboardButton(text="Назад", callback_data="global:main")],
    ]
    await call.message.edit_text("<b>Сжатие видео</b>\n\nВыберите вариант:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()

@video_router.message(Command("compress"))
async def cmd_compress(msg: types.Message, state: FSMContext):
    uid = msg.from_user.id
    if uid in active_compress_jobs: await msg.answer("Уже есть активная задача."); return
    args = msg.text.split()[1:]
    if len(args) >= 2:
        url = args[0]
        try:
            tmb = float(args[1]); codec = "h264"
            if len(args) >= 3:
                c = args[2].lower()
                if c in ("h265","hevc"): codec = "h265"
                elif c in ("av1","svtav1"): codec = "av1"
            await state.update_data(compress_url=url, compress_size=tmb, compress_codec=codec)
            task = asyncio.create_task(_start_compression(msg, state, uid))
            active_compress_tasks[uid] = task; return
        except ValueError: pass
    kb = [
        [InlineKeyboardButton(text="FFmpeg (локальное)", callback_data="vid:cv1")],
        [InlineKeyboardButton(text="ezgif.com (облачное)", callback_data="vid:cv2")],
        [InlineKeyboardButton(text="Назад", callback_data="global:main")],
    ]
    await msg.answer("<b>Сжатие видео</b>\n\nВыберите вариант:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# ── Compress V1: FFmpeg ──
@video_router.callback_query(F.data == "vid:cv1")
async def cv1_start(call: CallbackQuery, state: FSMContext):
    await call.answer(); await state.set_state(CompressStates.waiting_for_url)
    await call.message.edit_text("<b>Сжатие — FFmpeg</b>\n\nОтправьте прямую ссылку на видео.\n\n<code>/compress ссылка размер_мб [h264|h265|av1]</code>\n\n/cancel — отмена", parse_mode=ParseMode.HTML)

@video_router.message(CompressStates.waiting_for_url, F.text)
async def cv1_url(msg: types.Message, state: FSMContext):
    url = msg.text.strip()
    if url.startswith('/'): return
    if not is_url(url): await msg.answer("Не ссылка!"); return
    await state.update_data(compress_url=url); await state.set_state(CompressStates.waiting_for_size)
    kb = [[InlineKeyboardButton(text="25 МБ", callback_data="vid:cs_25"), InlineKeyboardButton(text="50 МБ", callback_data="vid:cs_50")],
          [InlineKeyboardButton(text="99 МБ", callback_data="vid:cs_99"), InlineKeyboardButton(text="200 МБ", callback_data="vid:cs_200")],
          [InlineKeyboardButton(text="500 МБ", callback_data="vid:cs_500"), InlineKeyboardButton(text="Свой", callback_data="vid:cs_custom")],
          [InlineKeyboardButton(text="Отмена", callback_data="vid:cs_cancel")]]
    await msg.answer("Целевой размер:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@video_router.callback_query(F.data.startswith("vid:cs_"))
async def cv1_size(call: CallbackQuery, state: FSMContext):
    await call.answer(); d = call.data
    if d == "vid:cs_cancel": await state.clear(); await call.message.edit_text("Отменено"); return
    if d == "vid:cs_custom": await state.set_state(CompressStates.waiting_for_custom_size); await call.message.edit_text("Введите размер в МБ:"); return
    sm = {"vid:cs_25":25,"vid:cs_50":50,"vid:cs_99":99,"vid:cs_200":200,"vid:cs_500":500}
    tmb = sm.get(d, 99); await state.update_data(compress_size=tmb); await state.set_state(CompressStates.waiting_for_codec)
    kb = [[InlineKeyboardButton(text="H.264", callback_data="vid:cc_h264"), InlineKeyboardButton(text="H.265", callback_data="vid:cc_h265")], [InlineKeyboardButton(text="AV1", callback_data="vid:cc_av1")]]
    await call.message.edit_text(f"Размер: <b>{tmb} МБ</b>\nКодек:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@video_router.message(CompressStates.waiting_for_custom_size, F.text)
async def cv1_custom(msg: types.Message, state: FSMContext):
    try: tmb = float(msg.text.strip())
    except ValueError: await msg.answer("Введите число."); return
    if tmb <= 0: await msg.answer("Положительное число!"); return
    await state.update_data(compress_size=tmb); await state.set_state(CompressStates.waiting_for_codec)
    kb = [[InlineKeyboardButton(text="H.264", callback_data="vid:cc_h264"), InlineKeyboardButton(text="H.265", callback_data="vid:cc_h265")], [InlineKeyboardButton(text="AV1", callback_data="vid:cc_av1")]]
    await msg.answer(f"Размер: <b>{tmb} МБ</b>\nКодек:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@video_router.callback_query(F.data.startswith("vid:cc_"))
async def cv1_codec(call: CallbackQuery, state: FSMContext):
    await call.answer()
    cm = {"vid:cc_h264":"h264","vid:cc_h265":"h265","vid:cc_av1":"av1"}
    codec = cm.get(call.data, "h264"); await state.update_data(compress_codec=codec)
    uid = call.from_user.id
    cn = {"h264":"H.264","h265":"H.265","av1":"AV1"}
    sd = await state.get_data()
    await call.message.edit_text(f"Запускаю: {sd.get('compress_size')} МБ / {cn.get(codec)}")
    task = asyncio.create_task(_start_compression(call.message, state, uid))
    active_compress_tasks[uid] = task

async def _start_compression(message, state, uid):
    sd = await state.get_data()
    url = sd.get('compress_url')
    tmb = sd.get('compress_size')
    codec = sd.get('compress_codec', 'h264')
    await state.clear()
    cn = {"h264": "H.264", "h265": "H.265", "av1": "AV1"}
    cl = cn.get(codec, codec.upper())
    if len(active_compress_jobs) >= MAX_CONCURRENT_JOBS:
        await message.answer("Сервер занят. Попробуйте позже.")
        return
    active_compress_jobs[uid] = {"status": "Запуск", "start_time": time.time(), "url": url, "target_mb": tmb, "codec": codec}

    status = await message.answer(
        status_box("FFmpeg Compress", [
            f"Цель: <b>{tmb} МБ</b> | Кодек: <b>{cl}</b>",
            f"",
            step_indicator(1, 4, "Скачиваю видео..."),
        ]),
        parse_mode=ParseMode.HTML,
    )
    tmpdir = tempfile.mkdtemp(prefix="compress_")
    try:
        async with _job_semaphore:
            active_compress_jobs[uid]["status"] = "Скачиваю"
            inp = os.path.join(tmpdir, "input")
            try:
                async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as http:
                    async with http.stream("GET", url) as resp:
                        if resp.status_code != 200:
                            await status.edit_text(
                                status_box("\u274c Ошибка", [f"HTTP: {resp.status_code}"]),
                                parse_mode=ParseMode.HTML,
                            )
                            return
                        total = int(resp.headers.get("content-length", 0))
                        dl = 0
                        lu = 0
                        with open(inp, "wb") as f:
                            async for chunk in resp.aiter_bytes(chunk_size=DOWNLOAD_CHUNK_SIZE):
                                f.write(chunk)
                                dl += len(chunk)
                                if dl > MAX_DOWNLOAD_SIZE_MB * 1024 * 1024:
                                    await status.edit_text(
                                        status_box("\u274c Ошибка", [f"Файл > {MAX_DOWNLOAD_SIZE_MB} МБ"]),
                                        parse_mode=ParseMode.HTML,
                                    )
                                    return
                                now = time.time()
                                if now - lu > 3 and total > 0:
                                    pct = int(dl * 100 / total)
                                    try:
                                        await status.edit_text(
                                            status_box("FFmpeg Compress", [
                                                f"Цель: <b>{tmb} МБ</b> | Кодек: <b>{cl}</b>",
                                                f"",
                                                step_indicator(1, 4, "Скачиваю видео..."),
                                                f"<code>{progress_bar(pct)}</code>",
                                                f"{format_size(dl)} / {format_size(total)}",
                                            ]),
                                            parse_mode=ParseMode.HTML,
                                        )
                                    except Exception:
                                        pass
                                    lu = now
            except Exception as e:
                await status.edit_text(
                    status_box("\u274c Ошибка скачивания", [f"<code>{str(e)[:300]}</code>"]),
                    parse_mode=ParseMode.HTML,
                )
                return

            # Analyze
            active_compress_jobs[uid]["status"] = "Анализ"
            await status.edit_text(
                status_box("FFmpeg Compress", [
                    f"Цель: <b>{tmb} МБ</b> | Кодек: <b>{cl}</b>",
                    f"",
                    step_indicator(2, 4, "Анализирую видео..."),
                ]),
                parse_mode=ParseMode.HTML,
            )
            try:
                info = await get_video_info(inp)
            except Exception as e:
                await status.edit_text(
                    status_box("\u274c Ошибка анализа", [f"<code>{str(e)[:300]}</code>"]),
                    parse_mode=ParseMode.HTML,
                )
                return

            isz = os.path.getsize(inp)
            info_text = f"{format_size(isz)} | {format_duration(info['duration'])} | {info['width']}x{info['height']}"

            if isz <= tmb * 1024 * 1024:
                r = await upload_to_tempshare(inp)
                ru = r.get("raw_url", r.get("url", "N/A"))
                await status.edit_text(
                    status_box("\u2714 Уже подходит", [
                        f"Файл: {format_size(isz)} (цель: {tmb} МБ)",
                        f"",
                        f"<b>Ссылка:</b>",
                        f"<code>{ru}</code>",
                    ]),
                    parse_mode=ParseMode.HTML,
                )
                return

            # Compress
            active_compress_jobs[uid]["status"] = "Сжимаю"
            outp = os.path.join(tmpdir, "out.mp4")
            lp = [0.0]

            async def pcb(m):
                active_compress_jobs[uid]["status"] = m
                if time.time() - lp[0] > 4:
                    lp[0] = time.time()
                    try:
                        await status.edit_text(
                            status_box("FFmpeg Compress", [
                                f"Оригинал: <b>{info_text}</b>",
                                f"Цель: <b>{tmb} МБ</b> | Кодек: <b>{cl}</b>",
                                f"",
                                step_indicator(3, 4, m),
                            ]),
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass

            try:
                result = await compress_video(inp, outp, tmb, codec, pcb)
            except Exception as e:
                await status.edit_text(
                    status_box("\u274c Ошибка сжатия", [f"<code>{str(e)[:300]}</code>"]),
                    parse_mode=ParseMode.HTML,
                )
                return

            try:
                os.remove(inp)
            except Exception:
                pass

            osz = result["output_size"]
            cr = (1 - osz / result["input_size"]) * 100

            # Upload
            active_compress_jobs[uid]["status"] = "Загружаю"
            await status.edit_text(
                status_box("FFmpeg Compress", [
                    f"Сжато: {format_size(osz)}",
                    f"",
                    step_indicator(4, 4, "Загрузка на TempShare..."),
                ]),
                parse_mode=ParseMode.HTML,
            )
            try:
                ur = await upload_to_tempshare(outp)
            except Exception as e:
                await status.edit_text(
                    status_box("\u274c Ошибка загрузки", [f"<code>{str(e)[:300]}</code>"]),
                    parse_mode=ParseMode.HTML,
                )
                return

            ru = ur.get("raw_url", ur.get("url", "N/A"))
            elapsed = time.time() - active_compress_jobs[uid]["start_time"]
            kb = [[InlineKeyboardButton(text="Сжать ещё", callback_data="vid:compress")]]
            await status.edit_text(
                status_box("\u2714 Сжатие завершено (FFmpeg)", [
                    f"",
                    f"<b>{format_size(result['input_size'])}</b>  \u2192  <b>{format_size(osz)}</b>",
                    f"Сжатие: <b>{cr:.1f}%</b> | Кодек: {cl}",
                    f"Время: <b>{format_duration(elapsed)}</b>",
                    f"",
                    f"<b>Ссылка:</b>",
                    f"<code>{ru}</code>",
                    f"",
                    f"До: {format_expires(ur.get('expires', ''))}",
                ]),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            )
    except Exception as e:
        logger.exception("Compress error")
        try:
            await status.edit_text(
                status_box("\u274c Ошибка", [f"<code>{str(e)[:300]}</code>"]),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_compress_jobs.pop(uid, None)
        active_compress_tasks.pop(uid, None)
        shutil.rmtree(tmpdir, ignore_errors=True)

# ── Compress V2: ezgif ──
@video_router.callback_query(F.data == "vid:cv2")
async def cv2_start(call: CallbackQuery, state: FSMContext):
    await call.answer(); await state.set_state(EzgifStates.waiting_for_url)
    await call.message.edit_text("<b>Сжатие — ezgif.com</b>\n\nОтправьте прямую ссылку на видео.\n/cancel — отмена", parse_mode=ParseMode.HTML)

@video_router.message(EzgifStates.waiting_for_url, F.text)
async def ez_url(msg: types.Message, state: FSMContext):
    url = msg.text.strip()
    if url.startswith('/'): return
    if not is_url(url): await msg.answer("Не ссылка!"); return
    cid = msg.chat.id; ezgif_pending[cid] = {"url": url}
    await state.set_state(EzgifStates.waiting_for_resolution)
    rk = [[InlineKeyboardButton(text=l, callback_data=f"vid:ezr:{k}")] for k, l in RESOLUTIONS.items()]
    await msg.answer("Разрешение:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rk))

@video_router.callback_query(F.data.startswith("vid:ezr:"))
async def ez_res(call: CallbackQuery, state: FSMContext):
    cid = call.message.chat.id; res = call.data.split(":")[-1]
    if cid not in ezgif_pending: await call.answer("Сначала ссылку!", show_alert=True); return
    ezgif_pending[cid]["resolution"] = res; await state.set_state(EzgifStates.waiting_for_bitrate)
    bk = [[InlineKeyboardButton(text=l, callback_data=f"vid:ezb:{k}")] for k, l in BITRATES.items()]
    await call.message.edit_text(f"Разрешение: <b>{RESOLUTIONS.get(res,res)}</b>\nБитрейт:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=bk))
    await call.answer()

@video_router.callback_query(F.data.startswith("vid:ezb:"))
async def ez_br(call: CallbackQuery, state: FSMContext):
    cid = call.message.chat.id; br = call.data.split(":")[-1]
    if cid not in ezgif_pending: await call.answer("Сначала ссылку!", show_alert=True); return
    ezgif_pending[cid]["bitrate"] = br; await state.set_state(EzgifStates.waiting_for_format)
    fk = [[InlineKeyboardButton(text=l, callback_data=f"vid:ezf:{k}")] for k, l in EZGIF_FORMATS.items()]
    await call.message.edit_text(f"Разрешение: <b>{RESOLUTIONS.get(ezgif_pending[cid].get('resolution',''),'')}</b>\nБитрейт: <b>{BITRATES.get(br,br)}</b>\nФормат:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=fk))
    await call.answer()

@video_router.callback_query(F.data.startswith("vid:ezf:"))
async def ez_fmt(call: CallbackQuery, state: FSMContext):
    cid = call.message.chat.id; fmt = call.data.split(":")[-1]
    if cid not in ezgif_pending: await call.answer("Сначала ссылку!", show_alert=True); return
    pend = ezgif_pending.pop(cid); await state.clear(); await call.answer("Запуск...")
    url = pend["url"]; res = pend.get("resolution","original"); br = int(pend.get("bitrate","500"))
    rl = RESOLUTIONS.get(res,res); bl = BITRATES.get(str(br),f"{br}kbps"); fl = EZGIF_FORMATS.get(fmt,fmt)
    st = f"<b>Сжатие (ezgif)</b>\n{rl} / {bl} / {fl}"
    status = await call.message.edit_text(f"{st}\n\nЗагрузка на ezgif...", parse_mode=ParseMode.HTML)
    start_time = time.monotonic()
    tmpdir = tempfile.mkdtemp(prefix="ezgif_")
    try:
        # Check if we need to split
        file_size_mb = 0
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
                resp = await http.head(url)
                cl = int(resp.headers.get("content-length", 0))
                file_size_mb = cl / (1024*1024)
        except Exception: pass

        if file_size_mb > EZGIF_CHUNK_MB:
            # Large file: download, split, process chunks
            await status.edit_text(f"{st}\n\nФайл {file_size_mb:.0f} МБ > {EZGIF_CHUNK_MB} МБ, скачиваю для разбивки...", parse_mode=ParseMode.HTML)
            local = os.path.join(tmpdir, "input.mp4")
            async with httpx.AsyncClient(timeout=900.0, follow_redirects=True) as http:
                async with http.stream("GET", url) as resp:
                    with open(local, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=DOWNLOAD_CHUNK_SIZE): f.write(chunk)
            actual_mb = os.path.getsize(local) / (1024*1024)
            # Split
            cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", local]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await proc.communicate()
            dur = float(json.loads(stdout.decode())["format"]["duration"])
            nc = max(1, math.ceil(actual_mb / EZGIF_CHUNK_MB))
            if nc <= 1:
                chunks = [local]
            else:
                cd = dur / nc; chunks = []
                for i in range(nc):
                    cp = os.path.join(tmpdir, f"chunk_{i:03d}.mp4")
                    await run_ffmpeg(['ffmpeg', '-y', '-ss', str(i*cd), '-i', local, '-t', str(cd), '-c', 'copy', '-avoid_negative_ts', '1', cp], f"split {i}")
                    if os.path.exists(cp) and os.path.getsize(cp) > 0: chunks.append(cp)
            # Process each chunk through ezgif
            loop = asyncio.get_running_loop()
            compressed_parts = []
            for idx, cp in enumerate(chunks):
                await status.edit_text(f"{st}\n\nЧасть {idx+1}/{len(chunks)}: загрузка на ezgif...", parse_mode=ParseMode.HTML)
                r1 = await loop.run_in_executor(ezgif_executor, lambda p=cp: requests.post(EZGIF_COMPRESS_URL, files={"new-image": ("video.mp4", open(p, "rb"), "video/mp4")}, data={"upload": "Upload video!"}, headers=EZGIF_HEADERS, timeout=300))
                soup = BeautifulSoup(r1.text, "html.parser")
                fi = soup.find("input", {"name": "file"})
                if not fi: raise Exception(f"Chunk {idx+1}: no file ID")
                fid = fi["value"]; form = soup.find("form", class_="ajax-form")
                action = form["action"] if form and form.get("action") else f"{EZGIF_COMPRESS_URL}/{fid}"
                await status.edit_text(f"{st}\n\nЧасть {idx+1}/{len(chunks)}: сжатие...", parse_mode=ParseMode.HTML)
                r2 = await loop.run_in_executor(ezgif_executor, lambda: ezgif_step2_compress(fid, action, requests.Session(), res, br, fmt))
                await status.edit_text(f"{st}\n\nЧасть {idx+1}/{len(chunks)}: скачивание...", parse_mode=ParseMode.HTML)
                dp_path = await loop.run_in_executor(ezgif_executor, lambda: ezgif_step3_download(r2["save_url"], r2["session"]))
                compressed_parts.append(dp_path)
            # Concat if multiple
            if len(compressed_parts) > 1:
                await status.edit_text(f"{st}\n\nСклеиваю {len(compressed_parts)} частей...", parse_mode=ParseMode.HTML)
                final = os.path.join(tmpdir, f"final.{fmt}")
                lf = os.path.join(tmpdir, "list.txt")
                with open(lf, "w") as f:
                    for p in compressed_parts: f.write(f"file '{p}'\n")
                await run_ffmpeg(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', lf, '-c', 'copy', final], "concat ezgif")
            else:
                final = compressed_parts[0]
            await status.edit_text(f"{st}\n\nЗагрузка на TempShare...", parse_mode=ParseMode.HTML)
            tr = await loop.run_in_executor(ezgif_executor, lambda: ezgif_step4_upload_tempshare(final))
            tt = time.monotonic() - start_time; fmb = os.path.getsize(final)/(1024*1024)
            ru = tr.get("raw_url", tr.get("url","N/A"))
            kb = [[InlineKeyboardButton(text="Сжать ещё", callback_data="vid:compress")]]
            await status.edit_text(f"<b>Готово! (ezgif, {len(chunks)} частей)</b>\n\n{rl} / {bl} / {fl}\n{actual_mb:.1f} МБ -> {fmb:.1f} МБ\nВремя: <b>{_fmt_time(tt)}</b>\n\n<code>{ru}</code>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        else:
            # Small file: direct URL upload to ezgif
            loop = asyncio.get_running_loop()
            r1 = await loop.run_in_executor(ezgif_executor, lambda: ezgif_step1_upload(url))
            await status.edit_text(f"{st}\n\nСжатие...", parse_mode=ParseMode.HTML)
            r2 = await loop.run_in_executor(ezgif_executor, lambda: ezgif_step2_compress(r1["file_id"], r1["action_url"], r1["session"], res, br, fmt))
            await status.edit_text(f"{st}\n\nСкачивание...", parse_mode=ParseMode.HTML)
            dp_path = await loop.run_in_executor(ezgif_executor, lambda: ezgif_step3_download(r2["save_url"], r2["session"]))
            await status.edit_text(f"{st}\n\nЗагрузка на TempShare...", parse_mode=ParseMode.HTML)
            tr = await loop.run_in_executor(ezgif_executor, lambda: ezgif_step4_upload_tempshare(dp_path))
            tt = time.monotonic() - start_time
            ru = tr.get("raw_url", tr.get("url","N/A"))
            kb = [[InlineKeyboardButton(text="Сжать ещё", callback_data="vid:compress")]]
            await status.edit_text(f"<b>Готово! (ezgif)</b>\n\n{rl} / {bl} / {fl}\n{r2.get('file_info','')}\nВремя: <b>{_fmt_time(tt)}</b>\n\n<code>{ru}</code>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except Exception as e:
        tt = time.monotonic() - start_time; logger.error(f"ezgif error: {e}", exc_info=True)
        kb = [[InlineKeyboardButton(text="Попробовать снова", callback_data="vid:compress")]]
        await status.edit_text(f"Ошибка ezgif ({tt:.1f}с):\n<code>{e}</code>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ══════════════ VIDEO/DOCUMENT HANDLERS (for merge) ══════════════

@video_router.message(F.video)
async def handle_video(msg: types.Message, state: FSMContext):
    uid = msg.from_user.id; d = get_merge_data(uid); v = msg.video
    mb = (v.file_size or 0) / (1024*1024)
    if mb > 20: await msg.answer(f"Видео {mb:.1f} МБ > 20 МБ (лимит Telegram API).\nИспользуйте /compress по ссылке."); return
    try:
        f = await bot.get_file(v.file_id)
        vn = f"video_{len(d.videos)+1}_{uuid.uuid4().hex[:8]}.mp4"
        vp = os.path.join(d.temp_dir, vn)
        await bot.download_file(f.file_path, vp)
        if not await is_valid_video(vp): os.remove(vp); await msg.answer("Видео повреждено."); return
        d.videos.append(vp)
        await msg.answer(f"Видео #{len(d.videos)} добавлено!\nВсего: {len(d.videos)}\n/merge_now — склеить")
    except Exception as e: logger.error(f"Video dl error: {e}"); await msg.answer("Не удалось скачать видео.")

@video_router.message(F.document)
async def handle_doc(msg: types.Message, state: FSMContext):
    cs = await state.get_state()
    if cs == MergeStates.waiting_for_watermark: return
    uid = msg.from_user.id; d = get_merge_data(uid); doc = msg.document
    mt = doc.mime_type or ""; fn = doc.file_name or ""
    vext = ('.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv', '.m4v')
    if not (mt.startswith('video/') or fn.lower().endswith(vext)):
        if mt.startswith('image/'): await msg.answer("Изображение. Для водяного знака: /watermark")
        return
    mb = (doc.file_size or 0) / (1024*1024)
    if mb > 20: await msg.answer(f"Файл {mb:.1f} МБ > 20 МБ."); return
    try:
        f = await bot.get_file(doc.file_id)
        ext = Path(fn).suffix if fn else '.mp4'
        vn = f"video_{len(d.videos)+1}_{uuid.uuid4().hex[:8]}{ext}"
        vp = os.path.join(d.temp_dir, vn)
        await bot.download_file(f.file_path, vp)
        if not await is_valid_video(vp): os.remove(vp); await msg.answer("Файл повреждён."); return
        d.videos.append(vp)
        await msg.answer(f"Видео #{len(d.videos)} добавлено!\nВсего: {len(d.videos)}\n/merge_now — склеить")
    except Exception as e: logger.error(f"Doc dl error: {e}"); await msg.answer("Не удалось скачать файл.")

# ── Status ──
@video_router.message(Command("vstatus"))
async def cmd_vstatus(msg: types.Message):
    uid = msg.from_user.id; job = active_compress_jobs.get(uid)
    if not job: await msg.answer("Нет активных задач сжатия."); return
    elapsed = time.time() - job["start_time"]
    await msg.answer(f"<b>Активная задача</b>\nСтатус: {job['status']}\nВремя: {format_duration(elapsed)}\nЦель: {job.get('target_mb')} МБ | {job.get('codec','h264').upper()}", parse_mode=ParseMode.HTML)
