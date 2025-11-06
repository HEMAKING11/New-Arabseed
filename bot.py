# bot_simple_autorun_prompt.py
import os
import re
import time
import json
import asyncio
import logging
import uuid
from typing import Optional, Tuple, Dict, List
from urllib.parse import urlparse, unquote, urlunparse, quote

import requests
from bs4 import BeautifulSoup
import pyfiglet

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ================= Logging / Banner =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("arabseed-bot")

print("\x1b[1;31m" + pyfiglet.Figlet(font="banner3-D").renderText("ARABSD") + "\x1b[0m")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("عرّف متغير البيئة BOT_TOKEN.")

# ================= HTTP defaults =================
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

# ================= In-memory store لتفادي طول callback_data =================
# نخزن URL في ماب قصيرة العمر (جلسة البوت)
PENDING: Dict[str, str] = {}  # token -> url

# ================= Helpers (من سكربتك مع تنضيف) =================
def extract_base_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"

def extract_title_from_url(url: str) -> str:
    parsed_url = urlparse(url)
    path = unquote(parsed_url.path)
    path_parts = path.strip('/').split('-')
    title = ' '.join(path_parts).replace('.html', '').title()
    if title.startswith("مسلسل"):
        words = title.split()
        new_title = []
        for word in words:
            new_title.append(word)
            if any(ch.isdigit() for ch in word):
                break
        title = ' '.join(new_title)
    return title

def follow_redirect(url, session=None, headers=None, timeout=10) -> Optional[str]:
    if session is None:
        session = requests.Session()
    if headers is None:
        headers = DEFAULT_HEADERS
    try:
        r = session.get(url, headers=headers, allow_redirects=False, timeout=timeout)
        if 'location' in r.headers:
            return r.headers['location']
        r2 = session.get(url, headers=headers, allow_redirects=True, timeout=timeout)
        return r2.url
    except Exception:
        return None

def get_download_info(server_href: str, referer: str) -> Optional[Dict[str, str]]:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    if referer:
        session.headers.update({"Referer": referer})
    try:
        redirected = follow_redirect(server_href, session=session)
        if not redirected:
            return None

        r_link = None
        if '?r=' in redirected:
            r_link = redirected
        else:
            tmp = session.get(redirected, timeout=12)
            m = re.search(r'(https?://[^"\'>\s]+/category/downloadz/\?r=\d+[^"\'>\s]*)', tmp.text)
            if m:
                r_link = m.group(1)
            elif '?r=' in tmp.url:
                r_link = tmp.url
            elif 'location' in tmp.headers and '?r=' in tmp.headers['location']:
                r_link = tmp.headers['location']
        if not r_link:
            return None

        rpage = session.get(r_link, timeout=12)
        rsoup = BeautifulSoup(rpage.text, 'html.parser')

        btn_tag = rsoup.find('a', id='btn') or rsoup.select_one('a.downloadbtn') or rsoup.find('a', class_='downloadbtn')
        final_asd_url = None
        if btn_tag and btn_tag.get('href'):
            candidate = btn_tag.get('href')
            if candidate.startswith('/'):
                candidate = extract_base_url(r_link) + candidate
            final_asd_url = candidate
        else:
            dynamic_param_pattern = r'([?&][a-zA-Z0-9_]+\d*=[^"&\']+)'
            qs_matches = re.findall(dynamic_param_pattern, rpage.text)
            params = []
            for q in qs_matches:
                normalized_param = q.lstrip('?&')
                if normalized_param.lower().startswith('r='):
                    continue
                pname = normalized_param.split('=', 1)[0]
                if not any(p.startswith(pname + '=') for p in params):
                    params.append(normalized_param)
            if params:
                sep = '&' if '?' in r_link else '?'
                final_asd_url = r_link + sep + '&'.join(params)
        if not final_asd_url:
            final_asd_url = r_link

        final_resp = session.get(final_asd_url, timeout=15)
        fsoup = BeautifulSoup(final_resp.text, 'html.parser')

        final_tag = fsoup.find('a', id='btn') or fsoup.find('a', class_='downloadbtn') or fsoup.find('a', href=re.compile(r'\.mp4'))
        if not final_tag:
            return None

        file_link = final_tag.get('href')
        if file_link and file_link.startswith('/'):
            file_link = extract_base_url(final_asd_url) + file_link

        file_name = None
        file_size = None
        name_span = fsoup.select_one('.TitleCenteral h3 span')
        if name_span:
            file_name = name_span.get_text(strip=True)
        size_span = fsoup.select_one('.TitleCenteral h3:nth-of-type(2) span')
        if size_span:
            file_size = size_span.get_text(strip=True)

        if not file_size:
            h3 = fsoup.find('h3')
            if h3:
                msize = re.search(r'الحجم[:\s\-–]*([\d\.,]+\s*(?:MB|GB))', h3.get_text())
                if msize:
                    file_size = msize.group(1)

        if not file_name:
            file_name = os.path.basename(file_link) if file_link else "unknown"

        return {
            'direct_link': file_link.replace(" ", ".") if file_link else None,
            'file_name': file_name,
            'file_size': file_size or "Unknown"
        }
    except Exception:
        return None

def find_last_numeric_segment_in_path(path_unquoted: str):
    parts = path_unquoted.strip('/').split('-')
    for i in range(len(parts)-1, -1, -1):
        if re.fullmatch(r'\d+', parts[i]):
            return i, parts[i]
    return None, None

def build_episode_url_from_any(url: str, episode_number: int) -> Optional[str]:
    p = urlparse(url)
    path_unquoted = unquote(p.path)
    idx, _ = find_last_numeric_segment_in_path(path_unquoted)
    if idx is None:
        return None
    parts = path_unquoted.strip('/').split('-')[:idx+1]
    parts[-1] = str(episode_number)
    new_path = '/' + '-'.join(parts)
    quoted_path = quote(new_path, safe="/%")
    return urlunparse((p.scheme, p.netloc, quoted_path, '', '', ''))

def extract_episode_and_base(url: str):
    p = urlparse(url)
    path_unquoted = unquote(p.path)
    idx, num = find_last_numeric_segment_in_path(path_unquoted)
    if idx is None or num is None:
        return None, None
    return int(num), lambda ep: build_episode_url_from_any(url, ep)

def looks_like_series(url: str) -> bool:
    path = unquote(urlparse(url).path)
    return ('مسلسل' in path) or ('الحلقة' in path)

# ================ المعالجة الأساسية ================
def process_single_episode(arabseed_url: str) -> Tuple[Optional[bool], Optional[Dict]]:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    try:
        if '/l/' in arabseed_url or 'reviewrate.net' in arabseed_url:
            arabseed_url = follow_redirect(arabseed_url, session=session) or arabseed_url

        try:
            resp = session.get(arabseed_url, timeout=12)
        except Exception:
            return None, None

        if resp.status_code == 404:
            return False, None
        if resp.status_code != 200:
            time.sleep(1.2)
            try:
                resp = session.get(arabseed_url, timeout=12)
            except Exception:
                return None, None
            if resp.status_code != 200:
                return False, None

        text_lower = resp.text.lower()
        if any(p in text_lower for p in ['لم يتم العثور', 'page not found', 'صفحة غير موجودة', 'not found']):
            return False, None

        soup = BeautifulSoup(resp.text, 'html.parser')
        download_anchor = soup.find('a', href=re.compile(r'/download/')) or soup.find('a', class_=re.compile(r'download__btn|downloadBTn'))
        if not download_anchor:
            return False, None

        quality_page_url = download_anchor.get('href')
        if quality_page_url.startswith('/'):
            quality_page_url = extract_base_url(arabseed_url) + quality_page_url
        base_url = extract_base_url(arabseed_url)

        try:
            qresp = session.get(quality_page_url, headers={'Referer': base_url + '/'}, timeout=12)
            if qresp.status_code != 200:
                return False, None
        except Exception:
            return None, None

        qsoup = BeautifulSoup(qresp.text, 'html.parser')
        server_links = qsoup.find_all('a', href=re.compile(r'/l/'))
        if not server_links:
            server_links = qsoup.select('ul.downloads__links__list a') or qsoup.find_all('a', class_=re.compile(r'download__item|arabseed'))
        if not server_links:
            return False, None

        referer = extract_base_url(quality_page_url) + "/"
        seen_qualities = set()
        buttons: List[List[InlineKeyboardButton]] = []

        for a in server_links:
            href = a.get('href')
            if not href:
                continue
            if 'arabseed' not in href and 'عرب سيد' not in a.get_text(" ", strip=True):
                continue

            quality = "Unknown"
            parent_with_quality = a.find_parent(attrs={"data-quality": True})
            if parent_with_quality:
                quality = parent_with_quality.get('data-quality')
            else:
                ptxt = a.get_text(" ", strip=True)
                qmatch = re.search(r'(\d{3,4}p)', ptxt)
                if qmatch:
                    quality = qmatch.group(1)
                else:
                    sq = a.find_previous('div', class_=re.compile(r'txt|text'))
                    if sq:
                        qmatch = re.search(r'(\d{3,4}p)', sq.get_text())
                        if qmatch:
                            quality = qmatch.group(1)

            if quality in seen_qualities:
                pass
            seen_qualities.add(quality)

            info = get_download_info(href, referer)
            if info and info.get('direct_link'):
                label = f"[ {info.get('file_size','?')} ]  •  {quality}"
                buttons.append([InlineKeyboardButton(text=label, url=info['direct_link'])])

        if not buttons:
            return False, None

        media_title = extract_title_from_url(arabseed_url)
        return True, {"title": media_title, "buttons": buttons}

    except Exception:
        return None, None

# ================ Telegram (aiogram v3) ================
HELP_TEXT = (
    "أهلًا يا نجم 👋 صلّي على النبي ﷺ\n"
    "ابعت لينك حلقة/فيلم وأنا أرجّعلك أزرار التحميل.\n\n"
    "لو اللينك حلقة مسلسل، هسألك تشغّل أوتورِن للحلقات اللي بعدها ولا لأ."
)

async def ask_autorun(message: Message, url: str):
    token = uuid.uuid4().hex[:16]
    PENDING[token] = url
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ أيوه، كمّل تلقائي", callback_data=f"auto:yes:{token}")],
        [InlineKeyboardButton(text="❌ لأ، الحلقة دي بس", callback_data=f"auto:no:{token}")],
    ])
    await message.answer("تشغّل أوتورِن للحلقات اللي بعدها؟", reply_markup=kb)

async def handle_yes(callback: CallbackQuery, url: str):
    await callback.message.edit_text("تمام ✅ هبدأ أوتورِن… هيوصلك الحلقات واحدة ورا التانية.")
    async def worker():
        current_num, builder = extract_episode_and_base(url)
        if current_num is None or builder is None:
            await callback.message.answer("مش لاقي رقم الحلقة في اللينك—أوتورِن اتلغى.")
            return
        while True:
            candidate_url = builder(current_num)
            if not candidate_url:
                await callback.message.answer(f"فشلت أبني لينك الحلقة {current_num} — هوقف.")
                break
            result, payload = await asyncio.to_thread(process_single_episode, candidate_url)
            if result is True and payload:
                txt = (
                    "⭕ تــحـــمــيـــل عـــــــرب ســـيـــــد مـبــــاشـــــر 🗂\n"
                    "ـ━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⌯ {payload['title']}\n"
                    f"📺 الحلقة: {current_num}\n\n"
                    "📁 اختر جودة التحميل:"
                )
                await callback.message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=payload["buttons"]))
                current_num += 1
                await asyncio.sleep(1.0)
            elif result is False:
                await callback.message.answer(f"مفيش حلقة {current_num} أو مفيش روابط—أوتورن وقف ✅")
                break
            else:
                await asyncio.sleep(1.2)
                result2, payload2 = await asyncio.to_thread(process_single_episode, candidate_url)
                if result2 is True and payload2:
                    txt = (
                        "⭕ تــحـــمــيـــل عـــــــرب ســـيـــــد مـبــــاشـــــر 🗂\n"
                        "ـ━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⌯ {payload2['title']}\n"
                        f"📺 الحلقة: {current_num}\n\n"
                        "📁 اختر جودة التحميل:"
                    )
                    await callback.message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=payload2["buttons"]))
                    current_num += 1
                    await asyncio.sleep(1.0)
                else:
                    await callback.message.answer(f"حصل خطأ أثناء معالجة الحلقة {current_num} — أوتورِن وقف.")
                    break
    asyncio.create_task(worker())

async def handle_no(callback: CallbackQuery, url: str):
    await callback.message.edit_text("تمام ✅ هعالج الحلقة دي بس…")
    result, payload = await asyncio.to_thread(process_single_episode, url)
    if result is True and payload:
        msg = (
            "⭕ تــحـــمــيـــل عـــــــرب ســـيـــــد مـبــــاشـــــر 🗂\n"
            "ـ━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⌯ {payload['title']}\n\n"
            "📁 اختر جودة التحميل:"
        )
        await callback.message.answer(msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=payload["buttons"]))
    elif result is False:
        await callback.message.answer("مش لاقي روابط للينك ده أو الحلقة مش متاحة.")
    else:
        await callback.message.answer("حصل خطأ مؤقت أثناء المعالجة. جرّب تاني.")

# ================ Handlers ================
async def cmd_start(message: Message):
    await message.answer("يا معلم… صلّي على النبي ﷺ ✨\n" + HELP_TEXT)

async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)

async def url_handler(message: Message):
    text = (message.text or "").strip()
    if not text.startswith("http"):
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    if looks_like_series(text):
        await ask_autorun(message, text)
    else:
        # فيلم أو حلقة من غير رقم—نرجّع الروابط مباشرة
        result, payload = await asyncio.to_thread(process_single_episode, text)
        if result is True and payload:
            msg = (
                "⭕ تــحـــمــيـــل عـــــــرب ســـيـــــد مـبــــاشـــــر 🗂\n"
                "ـ━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⌯ {payload['title']}\n\n"
                "📁 اختر جودة التحميل:"
            )
            await message.answer(msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=payload["buttons"]))
        elif result is False:
            await message.answer("مش لاقي روابط للينك ده أو المحتوى مش متاح.")
        else:
            await message.answer("حصل خطأ مؤقت أثناء المعالجة. جرّب تاني.")

async def callback_router(cb: CallbackQuery):
    data = cb.data or ""
    if not data.startswith("auto:"):
        await cb.answer()
        return
    _, choice, token = data.split(":", 2)
    url = PENDING.pop(token, None)
    if not url:
        await cb.message.edit_text("انتهت صلاحية الطلب، ابعت اللينك تاني.")
        await cb.answer()
        return
    await cb.answer()
    if choice == "yes":
        await handle_yes(cb, url)
    else:
        await handle_no(cb, url)

# ================ Run ================
async def main():
    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(url_handler, F.text)
    dp.callback_query.register(callback_router, F.data.startswith("auto:"))
    logging.info("Bot is up. Polling…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
