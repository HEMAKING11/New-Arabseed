import os
import json
import re
import time
import logging
import asyncio
import traceback
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, unquote, urlunparse, quote

import aiohttp
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ----------------- إعدادات التخزين -----------------
class Storage:
    """تخزين بيانات المستخدمين"""
    def __init__(self):
        self.user_sessions = {}
        self.processing_users = set()
        
    def is_processing(self, user_id: int) -> bool:
        return user_id in self.processing_users
        
    def set_processing(self, user_id: int, status: bool):
        if status:
            self.processing_users.add(user_id)
        else:
            self.processing_users.discard(user_id)
            
    def get_session(self, user_id: int) -> dict:
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = {
                'last_url': None,
                'last_title': None,
                'episode_number': None,
                'auto_mode': False,
                'history': []
            }
        return self.user_sessions[user_id]

storage = Storage()

# ----------------- إعدادات التسجيل (Logging) -----------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----------------- إعدادات البوت -----------------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = json.loads(os.environ.get("ADMIN_IDS", "[]"))
MAX_EPISODES_PER_RUN = 50
REQUEST_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ----------------- دوال مساعدة -----------------
def extract_base_url(url: str) -> str:
    """استخراج الرابط الأساسي"""
    parsed_url = urlparse(url)
    return f"{parsed_url.scheme}://{parsed_url.netloc}"

def extract_title_from_url(url: str) -> str:
    """استخراج العنوان من الرابط"""
    parsed_url = urlparse(url)
    path = unquote(parsed_url.path)
    path_parts = path.strip('/').split('-')
    title = ' '.join(path_parts).replace('.html', '').title()
    
    if title.startswith("مسلسل"):
        words = title.split()
        new_title = []
        for word in words:
            new_title.append(word)
            if any(char.isdigit() for char in word):
                break
        title = ' '.join(new_title)
    
    return title

async def follow_redirect(url: str, session: aiohttp.ClientSession, max_redirects: int = 5) -> Optional[str]:
    """تتبع عمليات إعادة التوجيه"""
    redirect_count = 0
    current_url = url
    
    while redirect_count < max_redirects:
        try:
            async with session.get(current_url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as response:
                if response.status in (301, 302, 303, 307, 308) and 'location' in response.headers:
                    redirect_count += 1
                    current_url = response.headers['location']
                    if not current_url.startswith(('http://', 'https://')):
                        base = extract_base_url(url)
                        current_url = base + current_url
                else:
                    return str(response.url)
        except Exception as e:
            logger.error(f"Error following redirect: {e}")
            return None
    
    return current_url

def find_last_numeric_segment_in_path(path_unquoted: str) -> Tuple[Optional[int], Optional[str]]:
    """إيجاد الجزء الرقمي الأخير في المسار"""
    parts = path_unquoted.strip('/').split('-')
    for i in range(len(parts)-1, -1, -1):
        if re.fullmatch(r'\d+', parts[i]):
            return i, parts[i]
    return None, None

def build_episode_url_from_any(url: str, episode_number: int) -> Optional[str]:
    """بناء رابط الحلقة"""
    p = urlparse(url)
    path_unquoted = unquote(p.path)
    idx, num = find_last_numeric_segment_in_path(path_unquoted)
    
    if idx is None:
        return None
    
    parts = path_unquoted.strip('/').split('-')[:idx+1]
    parts[-1] = str(episode_number)
    new_path = '/' + '-'.join(parts)
    quoted_path = quote(new_path, safe="/%")
    new_parsed = (p.scheme, p.netloc, quoted_path, '', '', '')
    return urlunparse(new_parsed)

def extract_episode_and_base(url: str) -> Tuple[Optional[int], Optional[callable]]:
    """استخراج رقم الحلقة ودالة البناء"""
    p = urlparse(url)
    path_unquoted = unquote(p.path)
    idx, num = find_last_numeric_segment_in_path(path_unquoted)
    
    if idx is None or num is None:
        return None, None
    
    return int(num), lambda ep: build_episode_url_from_any(url, ep)

# ----------------- دوال الاستخراج الرئيسية -----------------
async def get_download_info(server_href: str, referer: str) -> Optional[Dict]:
    """استخراج معلومات التحميل من رابط السيرفر"""
    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT, "Referer": referer},
            timeout=timeout
        ) as session:
            
            # تتبع إعادة التوجيه
            redirected = await follow_redirect(server_href, session)
            if not redirected:
                return None
            
            # البحث عن رابط ?r=
            r_link = None
            if '?r=' in redirected:
                r_link = redirected
            else:
                async with session.get(redirected) as response:
                    text = await response.text()
                    match = re.search(r'(https?://[^"\'>\s]+/category/downloadz/\?r=\d+[^"\'>\s]*)', text)
                    if match:
                        r_link = match.group(1)
                    elif '?r=' in str(response.url):
                        r_link = str(response.url)
            
            if not r_link:
                return None
            
            # تحليل صفحة التحميل
            async with session.get(r_link) as response:
                text = await response.text()
                soup = BeautifulSoup(text, 'html.parser')
                
                # البحث عن زر التحميل
                btn_tag = soup.find('a', id='btn') or soup.select_one('a.downloadbtn')
                final_asd_url = None
                
                if btn_tag and btn_tag.get('href'):
                    candidate = btn_tag.get('href')
                    if candidate.startswith('/'):
                        candidate = extract_base_url(r_link) + candidate
                    final_asd_url = candidate
                else:
                    # محاولة إنشاء الرابط ديناميكياً
                    dynamic_param_pattern = r'([?&][a-zA-Z0-9_]+\d*=[^"&\']+)'
                    qs_matches = re.findall(dynamic_param_pattern, text)
                    params = []
                    for q in qs_matches:
                        normalized_param = q.lstrip('?&')
                        if normalized_param.lower().startswith('r='):
                            continue
                        param_name = normalized_param.split('=', 1)[0]
                        if not any(p.startswith(param_name + '=') for p in params):
                            params.append(normalized_param)
                    
                    if params:
                        sep = '&' if '?' in r_link else '?'
                        final_asd_url = r_link + sep + '&'.join(params)
                
                if not final_asd_url:
                    final_asd_url = r_link
                
                # الحصول على الرابط النهائي
                async with session.get(final_asd_url) as final_resp:
                    final_text = await final_resp.text()
                    final_soup = BeautifulSoup(final_text, 'html.parser')
                    
                    # البحث عن رابط MP4
                    final_tag = (final_soup.find('a', id='btn') or 
                                final_soup.find('a', class_='downloadbtn') or 
                                final_soup.find('a', href=re.compile(r'\.mp4')))
                    
                    if not final_tag:
                        return None
                    
                    file_link = final_tag.get('href')
                    if file_link and file_link.startswith('/'):
                        file_link = extract_base_url(final_asd_url) + file_link
                    
                    # استخراج اسم الملف وحجمه
                    file_name = None
                    file_size = None
                    
                    name_span = final_soup.select_one('.TitleCenteral h3 span')
                    if name_span:
                        file_name = name_span.get_text(strip=True)
                    
                    size_span = final_soup.select_one('.TitleCenteral h3:nth-of-type(2) span')
                    if size_span:
                        file_size = size_span.get_text(strip=True)
                    
                    if not file_size:
                        h3 = final_soup.find('h3')
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
    
    except Exception as e:
        logger.error(f"Error in get_download_info: {e}")
        return None

async def process_arabseed_url(url: str, session: aiohttp.ClientSession) -> Tuple[bool, str, List[List[InlineKeyboardButton]]]:
    """معالجة رابط عرب سيد"""
    try:
        # فحص الرابط
        if not url.startswith(('http://', 'https://')):
            return False, "❌ رابط غير صالح!", []
        
        # تتبع الروابط المختصرة
        if '/l/' in url or 'reviewrate.net' in url:
            url = await follow_redirect(url, session) or url
        
        async with session.get(url, timeout=REQUEST_TIMEOUT) as response:
            if response.status != 200:
                return False, f"❌ الرابط غير متاح (رمز: {response.status})", []
            
            text = await response.text()
            
            # التحقق من وجود الصفحة
            if any(phrase in text.lower() for phrase in ['لم يتم العثور', 'page not found', 'صفحة غير موجودة']):
                return False, "❌ الحلقة غير موجودة!", []
            
            soup = BeautifulSoup(text, 'html.parser')
            
            # البحث عن رابط صفحة التحميل
            download_anchor = soup.find('a', href=re.compile(r'/download/')) or soup.find('a', class_=re.compile(r'download__btn|downloadBTn'))
            if not download_anchor:
                return False, "❌ لم أتمكن من العثور على روابط التحميل!", []
            
            quality_page_url = download_anchor.get('href')
            if quality_page_url.startswith('/'):
                quality_page_url = extract_base_url(url) + quality_page_url
            
            # زيارة صفحة الجودات
            async with session.get(quality_page_url, headers={'Referer': extract_base_url(url)}) as qresp:
                if qresp.status != 200:
                    return False, "❌ صفحة الجودات غير متاحة!", []
                
                qtext = await qresp.text()
                qsoup = BeautifulSoup(qtext, 'html.parser')
                
                # جمع روابط السيرفرات
                server_links = qsoup.find_all('a', href=re.compile(r'/l/'))
                if not server_links:
                    server_links = qsoup.select('ul.downloads__links__list a')
                
                if not server_links:
                    return False, "❌ لا توجد روابط تحميل متاحة!", []
                
                # معالجة كل سيرفر
                buttons = []
                seen_qualities = set()
                
                for a in server_links:
                    href = a.get('href')
                    if not href:
                        continue
                    
                    # تخطي الروابط غير المباشرة
                    if 'arabseed' not in href and 'عرب سيد' not in a.get_text(" ", strip=True):
                        continue
                    
                    # تحديد الجودة
                    quality = "Unknown"
                    parent_with_quality = a.find_parent(attrs={"data-quality": True})
                    if parent_with_quality:
                        quality = parent_with_quality.get('data-quality')
                    else:
                        ptxt = a.get_text(" ", strip=True)
                        qmatch = re.search(r'(\d{3,4}p)', ptxt)
                        if qmatch:
                            quality = qmatch.group(1)
                    
                    if quality in seen_qualities:
                        continue
                    seen_qualities.add(quality)
                    
                    # استخراج معلومات التحميل
                    info = await get_download_info(href, extract_base_url(quality_page_url))
                    if info and info.get('direct_link'):
                        btn_text = f"📥 {quality} ({info.get('file_size', '?')})"
                        buttons.append([InlineKeyboardButton(btn_text, url=info['direct_link'])])
                
                if not buttons:
                    return False, "❌ لم أتمكن من استخراج روابط التحميل!", []
                
                title = extract_title_from_url(url)
                return True, title, buttons
    
    except asyncio.TimeoutError:
        return False, "⏰ انتهى الوقت المحدد للطلب!", []
    except Exception as e:
        logger.error(f"Error processing URL: {e}\n{traceback.format_exc()}")
        return False, f"❌ حدث خطأ أثناء المعالجة: {str(e)}", []

# ----------------- معالجات Telegram -----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الأمر /start"""
    user = update.effective_user
    
    welcome_text = f"""
🎬 مرحباً {user.first_name}!
    
🤖 أنا بوت لتحميل حلقات عرب سيد مباشرة إلى تليجرام.

📌 *طريقة الاستخدام:*
1. أرسل لي رابط حلقة من موقع عرب سيد
2. سأرسل لك روابط التحميل المباشرة

⚡ *مميزات البوت:*
• تحميل مباشر بجودات متعددة
• دعم الروابط المختصرة
• واجهة سهلة الاستخدام
• يعمل 24/7

🔗 *مثال للرابط:*
https://arabseed.cam/مسلسل-العنكبوت-الحلقة-1.html

📢 *قناة البوت:* @ArabSeed_DL_Bot
    """
    
    keyboard = [
        [InlineKeyboardButton("🎬 ارسال رابط", switch_inline_query_current_chat="")],
        [InlineKeyboardButton("📢 قناة البوت", url="https://t.me/ArabSeed_DL_Bot")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الأمر /help"""
    help_text = """
📖 *مساعدة البوت:*

🔗 *كيفية الاستخدام:*
1. قم بنسخ رابط الحلقة من موقع عرب سيد
2. أرسل الرابط هنا في البوت
3. انتظر حتى أعالج الرابط
4. سأرسل لك روابط التحميل المباشرة

⚠️ *ملاحظات هامة:*
• البوت يدعم الروابط المباشرة فقط
• قد لا تعمل بعض الحلقات القديمة
• جودة التحميل تعتمد على المصدر الأصلي
• البوت لا يخزن أي ملفات على سيرفراته

📞 *للتواصل والدعم:* @ArabSeed_Support
    """
    
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الرسائل النصية"""
    user_id = update.effective_user.id
    message = update.message
    
    if storage.is_processing(user_id):
        await message.reply_text("⏳ جاري معالجة طلبك السابق، انتظر قليلاً...")
        return
    
    url = message.text.strip()
    
    if not url.startswith(('http://', 'https://')):
        await message.reply_text("❌ هذا ليس رابطاً صالحاً!")
        return
    
    storage.set_processing(user_id, True)
    
    try:
        # إرسال رسالة الانتظار
        wait_msg = await message.reply_text("⏳ جاري معالجة الرابط، يرجى الانتظار...")
        
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            success, title, buttons = await process_arabseed_url(url, session)
        
        if success:
            response_text = f"""
🎬 *{title}*

📥 *روابط التحميل المتاحة:*
اختر الجودة المناسبة من الأزرار أدناه.

🔔 *ملاحظة:* الروابط مباشرة من سيرفرات عرب سيد
            """
            
            keyboard = InlineKeyboardMarkup(buttons + [
                [InlineKeyboardButton("🔄 معالجة رابط آخر", callback_data="new_link")],
                [InlineKeyboardButton("📢 قناة البوت", url="https://t.me/ArabSeed_DL_Bot")]
            ])
            
            await wait_msg.delete()
            await message.reply_text(response_text, reply_markup=keyboard, parse_mode='Markdown')
            
            # حفظ في التاريخ
            storage.get_session(user_id)['history'].append({
                'url': url,
                'title': title,
                'time': datetime.now().isoformat()
            })
        else:
            await wait_msg.delete()
            await message.reply_text(f"{title}\n\n⚠️ تأكد من صحة الرابط وحاول مرة أخرى.")
    
    except Exception as e:
        logger.error(f"Error in handle_message: {e}")
        await message.reply_text("❌ حدث خطأ أثناء المعالجة، حاول مرة أخرى.")
    
    finally:
        storage.set_processing(user_id, False)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة ضغطات الأزرار"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "new_link":
        await query.edit_message_text("🔄 أرسل رابط الحلقة الجديدة...")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إحصائيات البوت (للمشرفين فقط)"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط!")
        return
    
    stats_text = f"""
📊 *إحصائيات البوت:*

👥 المستخدمين النشطين: {len(storage.user_sessions)}
🔄 الطلبات قيد المعالجة: {len(storage.processing_users)}
⏰ وقت التشغيل: {time.strftime('%H:%M:%S', time.gmtime(time.time() - start_time))}

📅 آخر تحديث: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    """
    
    await update.message.reply_text(stats_text, parse_mode='Markdown')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج الأخطاء"""
    logger.error(f"Update {update} caused error {context.error}")
    
    if update and update.effective_message:
        await update.effective_message.reply_text("❌ حدث خطأ غير متوقع، يرجى المحاولة مرة أخرى.")

# ----------------- التشغيل الرئيسي -----------------
def main():
    """الدالة الرئيسية لتشغيل البوت"""
    global start_time
    start_time = time.time()
    
    print("🎬 بدء تشغيل بوت عرب سيد...")
    
    # إنشاء التطبيق
    application = Application.builder().token(TOKEN).build()
    
    # إضافة المعالجات
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # إضافة معالج الأخطاء
    application.add_error_handler(error_handler)
    
    # بدء التشغيل
    print("🤖 البوت يعمل الآن! اضغط Ctrl+C لإيقافه.")
    
    # التشغيل المستمر
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
