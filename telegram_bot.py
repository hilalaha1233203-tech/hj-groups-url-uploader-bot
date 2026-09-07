"""HJ GROUPS Telegram Media Collector with persistent button UI."""
from __future__ import annotations
import asyncio, json, os, re, tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError
from telegram_session_store import session_store
from playbook_client import PlaybookClient, PlaybookError

API_ID=int(os.getenv("TELEGRAM_API_ID","0")); API_HASH=os.getenv("TELEGRAM_API_HASH",""); BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
SESSION_STRING=os.getenv("TELEGRAM_SESSION_STRING","").strip()
ALLOWED_USER_IDS={int(v.strip()) for v in os.getenv("TELEGRAM_ALLOWED_USER_IDS","").split(",") if v.strip().isdigit() and int(v.strip())>0}
DEFAULT_DESTINATION=os.getenv("TELEGRAM_DESTINATION","").strip(); BRANDING=os.getenv("TELEGRAM_BRANDING","@hjgroups_1").strip()
BOT_DELETE_SECONDS=max(0,int(os.getenv("TELEGRAM_BOT_DELETE_SECONDS","3600"))); MAX_BULK_MESSAGES=max(1,int(os.getenv("TELEGRAM_MAX_BULK_MESSAGES","500")))
MAX_ACTIVE_JOBS=max(1,int(os.getenv("TELEGRAM_MAX_ACTIVE_JOBS","1"))); STATE_FILE=Path(os.getenv("TELEGRAM_STATE_FILE","telegram_media_state.json"))
ENV_DESTINATIONS={}
for item in os.getenv("TELEGRAM_USER_DESTINATIONS","").split("|"):
    if ":" in item:
        uid,dest=item.split(":",1)
        if uid.strip().isdigit() and dest.strip(): ENV_DESTINATIONS[uid.strip()]=dest.strip()
user_client=TelegramClient(StringSession(SESSION_STRING),API_ID,API_HASH); bot=Bot(token=BOT_TOKEN); dp=Dispatcher()
SEM=asyncio.Semaphore(MAX_ACTIVE_JOBS); DELETE_TASKS=set()
@dataclass
class Job:
    owner_id:int; source:Any; candidates:dict[int,Any]; destination:str; selected:set[int]
JOBS={}; LOGIN_PHONE=None; LOGIN_CODE_HASH=None; LOGIN_LOCK=asyncio.Lock(); PENDING={}

def menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔐 Login"),KeyboardButton(text="📱 Session")],
        [KeyboardButton(text="🔗 Scan Link"),KeyboardButton(text="📦 Bulk Range")],
        [KeyboardButton(text="🎯 Destination"),KeyboardButton(text="📋 Select Files")],
        [KeyboardButton(text="❌ Cancel"),KeyboardButton(text="🚪 Logout")],
        [KeyboardButton(text="ℹ️ Help")]],resize_keyboard=True,is_persistent=True,input_field_placeholder="Choose a function or send a Telegram link")
def allowed(uid): return bool(uid and uid in ALLOWED_USER_IDS)
def load_state():
    try:
        d=json.loads(STATE_FILE.read_text(encoding="utf-8")); return d if isinstance(d,dict) else {}
    except (OSError,ValueError,TypeError): return {}
STATE=load_state()
def save_state():
    try: STATE_FILE.write_text(json.dumps(STATE,indent=2),encoding="utf-8")
    except OSError: pass
def destination(uid): return (STATE.get(str(uid)) or ENV_DESTINATIONS.get(str(uid)) or DEFAULT_DESTINATION).strip()
def set_destination(uid,d): STATE[str(uid)]=d.strip(); save_state()
def size(n):
    if not n:return "Unknown"
    v=float(n)
    for u in ("B","KB","MB","GB","TB"):
        if v<1024 or u=="TB": return f"{int(v)} {u}" if u=="B" else f"{v:.1f} {u}"
        v/=1024
    return "Unknown"
def kind(m):
    media=getattr(m,"media",None)
    if media is None:return None
    if type(media).__name__=="MessageMediaPhoto":return "photo"
    doc=getattr(media,"document",None)
    if doc is None:return "other"
    mime=(getattr(doc,"mime_type","") or "").lower(); names={type(a).__name__ for a in (getattr(doc,"attributes",[]) or [])}
    if "DocumentAttributeAudio" in names or mime.startswith("audio/"):return "audio"
    if "DocumentAttributeVideo" in names or mime.startswith("video/"):return "video"
    if mime.startswith("image/"):return "photo"
    return "document"
def media_size(m): return int(getattr(getattr(getattr(m,"media",None),"document",None),"size",0) or 0)
def caption(m):
    parts=[]; original=(getattr(m,"message",None) or "").strip()
    if original:parts.append(original)
    parts.append(f"📦 File Size: {size(media_size(m))}")
    if BRANDING:parts.append(BRANDING)
    return "\n\n".join(parts)[:1024]
def parse_link(v):
    x=re.match(r"^https?://(?:www\.)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]{3,}))/([0-9]+)(?:\?.*)?$",v.strip())
    if not x:raise ValueError("Invalid Telegram message link.")
    private,username,mid=x.groups(); return (f"-100{private}" if private else f"@{username}"),int(mid)
async def flood(fn:Callable[[],Awaitable[Any]]):
    while True:
        try:return await fn()
        except FloodWaitError as e: await asyncio.sleep(max(1,int(getattr(e,"seconds",1)))+1)
async def saved_session(): return SESSION_STRING or await session_store.get()
async def rebuild(session):
    global user_client
    if user_client.is_connected():await user_client.disconnect()
    user_client=TelegramClient(StringSession(session),API_ID,API_HASH); await user_client.connect()
def scan_menu(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Audio",callback_data=f"pick:{uid}:audio"),InlineKeyboardButton(text="🎬 Video",callback_data=f"pick:{uid}:video")],
        [InlineKeyboardButton(text="📷 Photos",callback_data=f"pick:{uid}:photo"),InlineKeyboardButton(text="📄 Documents",callback_data=f"pick:{uid}:document")],
        [InlineKeyboardButton(text="⬇️ All files",callback_data=f"pick:{uid}:all")],[InlineKeyboardButton(text="☑️ Select individually",callback_data=f"individual:{uid}")],[InlineKeyboardButton(text="❌ Cancel",callback_data=f"cancel:{uid}")]])
def confirm_menu(uid): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Confirm",callback_data=f"confirm:{uid}")],[InlineKeyboardButton(text="❌ Cancel",callback_data=f"cancel:{uid}")]])
def individual_menu(job):
    rows=[]
    for mid,m in list(job.candidates.items())[:50]: rows.append([InlineKeyboardButton(text=f"{'☑️' if mid in job.selected else '⬜'} #{mid} {kind(m) or 'media'} {size(media_size(m))}"[:60],callback_data=f"toggle:{job.owner_id}:{mid}")])
    rows += [[InlineKeyboardButton(text="✅ Continue",callback_data=f"individual_confirm:{job.owner_id}")],[InlineKeyboardButton(text="❌ Cancel",callback_data=f"cancel:{job.owner_id}")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)
async def create_job(uid,source,messages,status):
    if not messages:return await status.edit_text("No downloadable media messages were found.",reply_markup=menu())
    dest=destination(uid)
    if not dest:return await status.edit_text("Set a destination first with 🎯 Destination.",reply_markup=menu())
    cand={m.id:m for m in messages}; JOBS[uid]=Job(uid,source,cand,dest,set(cand) if len(cand)==1 else set())
    await status.edit_text(f"🔎 Scan Complete\n\n📦 Files: {len(messages)}\n📦 Total size: {size(sum(media_size(m) for m in messages))}\n\nChoose what to process:",reply_markup=scan_menu(uid))
async def send_destination(m,dest):
    token=os.getenv("PLAYBOOK_API_TOKEN","").strip(); org=os.getenv("PLAYBOOK_ORG_SLUG","").strip()
    if not token or not org:return await flood(lambda:user_client.send_file(dest,m.media,caption=caption(m)))
    td=Path(os.getenv("TELEGRAM_TEMP_DIR",tempfile.gettempdir())); td.mkdir(parents=True,exist_ok=True); name=getattr(getattr(m,"file",None),"name",None) or f"telegram-{getattr(m,'id','media')}"; path=td/name
    try:
        await user_client.download_media(m,file=str(path)); client=PlaybookClient(token=token,org_slug=org); asset_token=await client.upload_file(path,title=name); asset={}
        for _ in range(30):
            asset=await client.get_asset(asset_token)
            if not asset.get("is_skeleton",False):break
            await asyncio.sleep(2)
        url=str(asset.get("display_url") or "").strip()
        if not url:raise PlaybookError(str(asset.get("source_error") or "Playbook asset has no display_url"))
        sent=await flood(lambda:user_client.send_file(dest,url,name=name,caption=caption(m)))
        try:await client.delete_asset(asset_token)
        except Exception:pass
        return sent
    finally:
        try:path.unlink(missing_ok=True)
        except OSError:pass
async def send_copy(m,uid):
    sent=await flood(lambda:user_client.send_file(uid,m.media,caption=caption(m)))
    if BOT_DELETE_SECONDS:
        async def later():
            await asyncio.sleep(BOT_DELETE_SECONDS)
            try:await user_client.delete_messages(sent.peer_id,[sent.id])
            except Exception:pass
        t=asyncio.create_task(later()); DELETE_TASKS.add(t); t.add_done_callback(DELETE_TASKS.discard)
async def process(job,status):
    async with SEM:
        selected=[job.candidates[mid] for mid in job.candidates if mid in job.selected]; done=failed=0
        for m in selected:
            try:await send_destination(m,job.destination); await send_copy(m,job.owner_id); done+=1
            except Exception as e:failed+=1; print(f"File {getattr(m,'id','?')} failed: {type(e).__name__}: {e}")
            await status.edit_text(f"⏳ Processing {done+failed}/{len(selected)} files...\n✅ Sent: {done}\n⚠️ Failed: {failed}")
        await status.edit_text(f"✅ Completed\n\nSent: {done}\nFailed: {failed}\n\nDestination: {job.destination}",reply_markup=menu())

@dp.message(CommandStart())
async def start(message:Message):
    uid=message.from_user.id if message.from_user else None
    if not allowed(uid):return
    await message.answer("HJ GROUPS Media Collector\n\nAll functions are available as buttons below.\n\n🔗 Scan Link — one media message\n📦 Bulk Range — multiple messages\n📋 Select Files — choose files\n🎯 Destination — delivery target\n🔐 Login / 📱 Session / 🚪 Logout — Telegram account controls",reply_markup=menu())
@dp.message(F.text=="ℹ️ Help")
async def help_button(m:Message): await start(m)
@dp.message(F.text=="🔗 Scan Link")
async def scan_button(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    PENDING[uid]="link"; await m.answer("🔗 Send the Telegram message link now.",reply_markup=menu())
@dp.message(F.text=="📦 Bulk Range")
async def bulk_button(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    PENDING[uid]="range"; await m.answer("📦 Send: @channel START_ID END_ID\nExample: @mychannel 100 150",reply_markup=menu())
@dp.message(F.text=="📋 Select Files")
async def select_button(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    PENDING[uid]="select"; await m.answer("📋 Send comma-separated message IDs.\nExample: 101,102,105",reply_markup=menu())
@dp.message(F.text=="🎯 Destination")
async def dest_button(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    PENDING[uid]="destination"; await m.answer("🎯 Send @username or chat ID.",reply_markup=menu())
@dp.message(F.text=="🔐 Login")
async def login_button(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    PENDING[uid]="login"; await m.answer("🔐 Send your Telegram phone number, e.g. +919876543210",reply_markup=menu())
@dp.message(F.text=="📱 Session")
async def session_button(m:Message): await session_cmd(m)
@dp.message(F.text=="🚪 Logout")
async def logout_button(m:Message): await logout_cmd(m)
@dp.message(F.text=="❌ Cancel")
async def cancel_button(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    JOBS.pop(uid,None); PENDING.pop(uid,None); await m.answer("❌ Current action cancelled.",reply_markup=menu())

@dp.message(Command("login"))
async def login_cmd(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    global LOGIN_PHONE,LOGIN_CODE_HASH
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2:return await m.answer("Usage: /login +91xxxxxxxxxx",reply_markup=menu())
    if not session_store.configured and not SESSION_STRING:return await m.answer("Session storage is not configured.",reply_markup=menu())
    async with LOGIN_LOCK:
        try:
            await user_client.connect(); sent=await user_client.send_code_request(p[1].strip()); LOGIN_PHONE=p[1].strip(); LOGIN_CODE_HASH=sent.phone_code_hash
            await m.answer("OTP sent. Reply with /otp 12345",reply_markup=menu())
        except Exception as e:await m.answer(f"Login failed: {type(e).__name__}: {e}",reply_markup=menu())
@dp.message(Command("otp"))
async def otp_cmd(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    global LOGIN_PHONE,LOGIN_CODE_HASH
    p=(m.text or "").split(maxsplit=1)
    if not LOGIN_PHONE or not LOGIN_CODE_HASH:return await m.answer("No login is waiting. Use 🔐 Login.",reply_markup=menu())
    if len(p)!=2:return await m.answer("Usage: /otp 12345",reply_markup=menu())
    try:
        await user_client.sign_in(LOGIN_PHONE,p[1].strip(),phone_code_hash=LOGIN_CODE_HASH); await session_store.set(user_client.session.save()); LOGIN_PHONE=LOGIN_CODE_HASH=None; PENDING.pop(uid,None); await m.answer("Telegram account login successful. ✅",reply_markup=menu())
    except SessionPasswordNeededError:await m.answer("2FA enabled. Reply with /2fa your_password",reply_markup=menu())
    except Exception as e:await m.answer(f"OTP login failed: {type(e).__name__}: {e}",reply_markup=menu())
@dp.message(Command("2fa"))
async def twofa_cmd(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    global LOGIN_PHONE,LOGIN_CODE_HASH
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2:return await m.answer("Usage: /2fa your_password",reply_markup=menu())
    try:await user_client.sign_in(password=p[1]); await session_store.set(user_client.session.save()); LOGIN_PHONE=LOGIN_CODE_HASH=None; PENDING.pop(uid,None); await m.answer("Telegram account login successful. ✅",reply_markup=menu())
    except Exception as e:await m.answer(f"2FA login failed: {type(e).__name__}: {e}",reply_markup=menu())
@dp.message(Command("logout"))
async def logout_cmd(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    try:
        if user_client.is_connected():await user_client.log_out()
    finally:await session_store.clear()
    PENDING.pop(uid,None); await m.answer("Telegram account logged out and saved session removed. ✅",reply_markup=menu())
@dp.message(Command("session"))
async def session_cmd(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    s=await saved_session()
    if not s:return await m.answer("No Telegram user session. Use 🔐 Login.",reply_markup=menu())
    try:
        if not user_client.is_connected():await rebuild(s)
        if await user_client.is_user_authorized():
            me=await user_client.get_me(); await m.answer(f"Session active: {getattr(me,'username',None) or me.id} ✅",reply_markup=menu())
        else:await m.answer("Session is not authorized. Use 🔐 Login.",reply_markup=menu())
    except Exception as e:await m.answer(f"Session check failed: {type(e).__name__}: {e}",reply_markup=menu())
@dp.message(Command("setdestination"))
async def setdest_cmd(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2:return await m.answer("Usage: /setdestination @channel_or_chat_id",reply_markup=menu())
    set_destination(uid,p[1]); PENDING.pop(uid,None); await m.answer(f"✅ Destination saved: {p[1]}",reply_markup=menu())
@dp.message(Command("cancel"))
async def cancel_cmd(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    JOBS.pop(uid,None);PENDING.pop(uid,None);await m.answer("❌ Current job cancelled.",reply_markup=menu())
@dp.message(Command("range"))
async def range_cmd(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    p=(m.text or "").split()
    if len(p)!=4:return await m.answer("Usage: /range @channel START_ID END_ID",reply_markup=menu())
    if not destination(uid):return await m.answer("Set a destination first with 🎯 Destination.",reply_markup=menu())
    try:
        source=await user_client.get_entity(p[1]); status=await m.answer("🔎 Scanning messages...",reply_markup=menu()); msgs=await user_client.get_messages(source,ids=list(range(int(p[2]),int(p[3])+1)))
        if len(msgs)>MAX_BULK_MESSAGES:raise ValueError(f"Maximum range is {MAX_BULK_MESSAGES} messages.")
        await create_job(uid,source,[x for x in msgs if x and getattr(x,"media",None)],status)
    except (ValueError,RPCError) as e:await m.answer(f"Could not scan range: {e}",reply_markup=menu())
@dp.message(Command("select"))
async def select_cmd(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    job=JOBS.get(uid); p=(m.text or "").split(maxsplit=1)
    if not job:return await m.answer("No active scan. Use 🔗 Scan Link or 📦 Bulk Range first.",reply_markup=menu())
    if len(p)!=2:return await m.answer("Usage: /select 25,31,44",reply_markup=menu())
    try:ids={int(v.strip()) for v in p[1].split(",") if v.strip()}
    except ValueError:return await m.answer("Message IDs must be comma-separated numbers.",reply_markup=menu())
    missing=ids-set(job.candidates)
    if missing:return await m.answer("IDs not in current scan: "+", ".join(map(str,sorted(missing)[:20])),reply_markup=menu())
    job.selected=ids; await m.answer(f"Selected {len(ids)} file(s).\nTotal size: {size(sum(media_size(job.candidates[i]) for i in ids))}\n\nDestination: {job.destination}\n\nConfirm?",reply_markup=confirm_menu(uid))
@dp.message(F.text)
async def text_handler(m:Message):
    uid=m.from_user.id if m.from_user else None
    if not allowed(uid):return
    text=(m.text or "").strip(); action=PENDING.get(uid)
    if action=="login":PENDING.pop(uid,None);return await login_cmd(m.model_copy(update={"text":f"/login {text}"}))
    if action=="destination":PENDING.pop(uid,None);return await setdest_cmd(m.model_copy(update={"text":f"/setdestination {text}"}))
    if action=="range":PENDING.pop(uid,None);return await range_cmd(m.model_copy(update={"text":f"/range {text}"}))
    if action=="select":PENDING.pop(uid,None);return await select_cmd(m.model_copy(update={"text":f"/select {text}"}))
    if not re.match(r"^https?://(?:www\.)?t\.me/",text):return await m.answer("Send a Telegram t.me message link or choose a button below.",reply_markup=menu())
    if not destination(uid):return await m.answer("Set a destination first with 🎯 Destination.",reply_markup=menu())
    status=await m.answer("🔎 Scanning message...",reply_markup=menu())
    try:
        peer,mid=parse_link(text); source=await user_client.get_entity(peer); msg=await user_client.get_messages(source,ids=mid)
        if not msg or not getattr(msg,"media",None):return await status.edit_text("That message does not contain downloadable media.",reply_markup=menu())
        await create_job(uid,source,[msg],status)
    except (ValueError,RPCError) as e:await status.edit_text(f"Could not resolve that message: {e}",reply_markup=menu())
@dp.callback_query(F.data.startswith("pick:"))
async def pick(cb:CallbackQuery):
    uid=cb.from_user.id
    if not allowed(uid):return await cb.answer("Not authorized",show_alert=True)
    _,owner,typ=cb.data.split(":",2);job=JOBS.get(int(owner))
    if not job or int(owner)!=uid:return await cb.answer("Job expired",show_alert=True)
    job.selected=set(job.candidates) if typ=="all" else {i for i,x in job.candidates.items() if kind(x)==typ}
    if not job.selected:return await cb.answer("No files of that type",show_alert=True)
    await cb.message.edit_text(f"Ready to process {len(job.selected)} file(s).\n\nTotal size: {size(sum(media_size(job.candidates[i]) for i in job.selected))}\n\nDestination: {job.destination}\n\nConfirm?",reply_markup=confirm_menu(uid));await cb.answer()
@dp.callback_query(F.data.startswith("individual:"))
async def individual(cb:CallbackQuery):
    uid=cb.from_user.id
    if not allowed(uid):return await cb.answer("Not authorized",show_alert=True)
    job=JOBS.get(int(cb.data.split(":",1)[1]))
    if not job or job.owner_id!=uid:return await cb.answer("Job expired",show_alert=True)
    job.selected=set();await cb.message.edit_text("Select individual files.\n\nSelected: 0",reply_markup=individual_menu(job));await cb.answer()
@dp.callback_query(F.data.startswith("toggle:"))
async def toggle(cb:CallbackQuery):
    uid=cb.from_user.id
    if not allowed(uid):return await cb.answer("Not authorized",show_alert=True)
    _,owner,mid=cb.data.split(":",2);job=JOBS.get(int(owner));mid=int(mid)
    if not job or job.owner_id!=uid or mid not in job.candidates:return await cb.answer("Job expired",show_alert=True)
    job.selected.remove(mid) if mid in job.selected else job.selected.add(mid);await cb.message.edit_reply_markup(reply_markup=individual_menu(job));await cb.answer(f"Selected: {len(job.selected)}")
@dp.callback_query(F.data.startswith("individual_confirm:"))
async def individual_confirm(cb:CallbackQuery):
    uid=cb.from_user.id;job=JOBS.get(int(cb.data.split(":",1)[1]))
    if not allowed(uid):return await cb.answer("Not authorized",show_alert=True)
    if not job or job.owner_id!=uid:return await cb.answer("Job expired",show_alert=True)
    if not job.selected:return await cb.answer("Select at least one file",show_alert=True)
    await cb.message.edit_text(f"Selected {len(job.selected)} file(s).\nTotal size: {size(sum(media_size(job.candidates[i]) for i in job.selected))}\n\nDestination: {job.destination}\n\nConfirm?",reply_markup=confirm_menu(uid));await cb.answer()
@dp.callback_query(F.data.startswith("confirm:"))
async def confirm(cb:CallbackQuery):
    uid=cb.from_user.id;job=JOBS.get(int(cb.data.split(":",1)[1]))
    if not allowed(uid):return await cb.answer("Not authorized",show_alert=True)
    if not job or job.owner_id!=uid:return await cb.answer("Job expired",show_alert=True)
    if not job.selected:return await cb.answer("Nothing selected",show_alert=True)
    await cb.answer("Started");
    try:await process(job,cb.message)
    finally:JOBS.pop(uid,None)
@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_callback(cb:CallbackQuery):
    uid=cb.from_user.id
    if not allowed(uid):return await cb.answer("Not authorized",show_alert=True)
    owner=int(cb.data.split(":",1)[1])
    if owner==uid:JOBS.pop(uid,None);PENDING.pop(uid,None)
    await cb.message.edit_text("❌ Cancelled.",reply_markup=menu());await cb.answer()

async def main():
    if not API_ID or not API_HASH or not BOT_TOKEN:raise RuntimeError("Set Telegram API credentials.")
    if not ALLOWED_USER_IDS:raise RuntimeError("Set TELEGRAM_ALLOWED_USER_IDS to at least one Telegram user ID.")
    s=await saved_session()
    if s:
        await rebuild(s)
        if await user_client.is_user_authorized():
            me=await user_client.get_me();print(f"Telethon connected as {getattr(me,'username',None) or me.id}")
    else:print("Telethon user session not configured; use /login")
    await dp.start_polling(bot)
if __name__=="__main__":asyncio.run(main())
