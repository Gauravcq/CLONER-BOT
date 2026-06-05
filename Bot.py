#!/usr/bin/env python3
# ╔══════════════════════════════════════════════════════════════════╗
# ║        UNIVERSAL TELEGRAM CLONER BOT — by Gourav Rajput         ║
# ║   Clones Groups / Channels / Forums / Topic Groups              ║
# ║   Server-side copy · No disk usage · Full topic support         ║
# ╚══════════════════════════════════════════════════════════════════╝

import asyncio
import os
import sys
import json
import re
import time
import logging
from typing import Dict, List, Optional, Tuple, Set, Union
from pathlib import Path
from dataclasses import dataclass, field

# ── Fix asyncio event loop BEFORE any third-party import ──────────
try:
    _loop = asyncio.get_event_loop()
    if _loop.is_closed():
        raise RuntimeError
except RuntimeError:
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

# ── Pyrogram ───────────────────────────────────────────────────────
from pyrogram import Client, filters, enums
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import (
    Message, Chat, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from pyrogram.errors import (
    FloodWait, RPCError,
    FileReferenceExpired, FileReferenceInvalid,
    ChatAdminRequired, ChannelPrivate,
    PeerIdInvalid, UsernameNotOccupied,
    InviteHashExpired, InviteHashInvalid,
    UserNotParticipant,
)
from pyrogram.raw.functions.channels import CreateForumTopic, GetForumTopics
from pyrogram.raw import types as raw_types

# ── pyromod (after loop fix) ───────────────────────────────────────
from pyromod import listen

# ══════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════
API_ID           = int(os.environ.get("API_ID", 0))
API_HASH         = os.environ.get("API_HASH", "")
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
STRING_SESSION   = os.environ.get("STRING_SESSION", "")
OWNER_ID         = int(os.environ.get("OWNER_ID", 0))
FORCESUB_CHANNEL = os.environ.get("FORCESUB_CHANNEL", "")

if not all([API_ID, API_HASH, BOT_TOKEN, STRING_SESSION, OWNER_ID]):
    print("MISSING ENV VARS: API_ID, API_HASH, BOT_TOKEN, STRING_SESSION, OWNER_ID")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("ClonerBot")

# ══════════════════════════════════════════════════════════════════
#  GLOBALS
# ══════════════════════════════════════════════════════════════════
bot:  Client = None
user: Client = None

active_jobs:  Dict[str, "CloneJob"] = {}
cancel_flags: Dict[str, bool]       = {}

# ══════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ══════════════════════════════════════════════════════════════════
@dataclass
class TopicMeta:
    id:            int
    title:         str
    icon_color:    int = 0
    icon_emoji_id: int = 0

@dataclass
class CloneJob:
    src_id:   int
    dst_id:   int
    src_name: str = ""
    dst_name: str = ""
    src_type: str = ""
    dst_type: str = ""
    topics:           List[Dict] = field(default_factory=list)
    completed_topics: Set[str]   = field(default_factory=set)
    last_msg_id:      Dict[str, int] = field(default_factory=dict)
    total_messages:   int   = 0
    cloned_messages:  int   = 0
    failed_messages:  int   = 0
    start_time:       float = 0.0

    _SAVE_DIR = Path("/tmp/clone_jobs")

    def _path(self):
        self._SAVE_DIR.mkdir(parents=True, exist_ok=True)
        return self._SAVE_DIR / f"{self.src_id}_{self.dst_id}.json"

    def save(self):
        d = {k: (list(v) if isinstance(v, set) else v)
             for k, v in self.__dict__.items() if not k.startswith("_")}
        self._path().write_text(json.dumps(d, indent=2))

    @classmethod
    def load(cls, src_id, dst_id):
        p = cls._SAVE_DIR / f"{src_id}_{dst_id}.json"
        if not p.exists():
            return None
        try:
            d  = json.loads(p.read_text())
            j  = cls(d["src_id"], d["dst_id"])
            for k, v in d.items():
                if k == "completed_topics":
                    setattr(j, k, set(v))
                elif k not in ("src_id", "dst_id"):
                    setattr(j, k, v)
            return j
        except Exception:
            return None

    def delete(self):
        p = self._path()
        if p.exists():
            p.unlink()

# ══════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════
def elapsed_str(sec: float) -> str:
    s = int(sec)
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def pbar(done: int, total: int, w=14) -> str:
    f = min(w, int(w * done / total)) if total else 0
    return "█" * f + "░" * (w - f)

def progress_text(job: CloneJob, topic: str, done: int, total: int) -> str:
    pct     = done / total * 100 if total else 0
    elapsed = time.time() - job.start_time
    avg     = done / elapsed if elapsed > 0 else 0
    eta     = (total - done) / avg if avg > 0 else 0
    lines = [
        f"📂 **{job.src_name}** ➠ **{job.dst_name}**",
        f"📌 Topic: `{topic}`",
        f"[`{pbar(done,total)}`] `{pct:.1f}%`",
        f"✅ `{done}` / `{total}`",
    ]
    if avg > 0:
        lines.append(f"⚡ `{avg:.1f} msg/s`  ⏳ ETA `{elapsed_str(eta)}`")
    overall = job.cloned_messages + done
    lines += [
        f"\n📊 Overall: `{overall}` / `{job.total_messages}`",
        f"⏰ Elapsed: `{elapsed_str(elapsed)}`",
    ]
    if job.failed_messages:
        lines.append(f"⚠️ Failed: `{job.failed_messages}`")
    return "\n".join(lines)

async def safe_edit(msg: Message, text: str):
    try:
        await msg.edit_text(text)
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════
#  CHAT RESOLUTION
# ══════════════════════════════════════════════════════════════════
async def resolve_chat(identifier: str) -> Optional[Chat]:
    ident = identifier.strip()
    m = re.search(r"(?:joinchat/|\+)([\w\-]+)", ident)
    if m:
        try:
            return await user.join_chat(m.group(1))
        except Exception:
            try:
                return await user.get_chat(m.group(1))
            except Exception:
                return None
    m = re.search(r"t\.me/([\w_]+)", ident)
    if m:
        ident = m.group(1)
    try:
        return await user.get_chat(int(ident))
    except ValueError:
        pass
    try:
        return await user.get_chat(ident.lstrip("@"))
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════════
#  FORUM TOPIC HELPERS
# ══════════════════════════════════════════════════════════════════
async def get_forum_topics(chat_id: int) -> List[TopicMeta]:
    topics = []
    try:
        peer = await user.resolve_peer(chat_id)
        offset_id = offset_topic = 0
        while True:
            r = await user.invoke(GetForumTopics(
                peer=peer, q="",
                offset_date=0, offset_id=offset_id,
                offset_topic=offset_topic, limit=100,
            ))
            if not r.topics:
                break
            for t in r.topics:
                if isinstance(t, raw_types.ForumTopic):
                    topics.append(TopicMeta(
                        id=t.id, title=t.title,
                        icon_color=getattr(t, "icon_color", 0),
                        icon_emoji_id=getattr(t, "icon_emoji_id", 0),
                    ))
            if len(r.topics) < 100:
                break
            last = r.topics[-1]
            offset_id = offset_topic = last.id
    except Exception as e:
        log.warning(f"get_forum_topics: {e}")
    return topics

async def create_topic(chat_id: int, title: str, icon_color: int = 0x6FB9F0) -> Optional[int]:
    try:
        peer = await user.resolve_peer(chat_id)
        r = await user.invoke(CreateForumTopic(
            channel=peer, title=title,
            icon_color=icon_color, random_id=user.rnd_id(),
        ))
        for upd in r.updates:
            if hasattr(upd, "message") and hasattr(upd.message, "id"):
                return upd.message.id
            if isinstance(upd, raw_types.UpdateMessageID):
                return upd.id
    except Exception as e:
        log.error(f"create_topic '{title}': {e}")
    return None

async def find_or_create_topic(chat_id: int, title: str, existing: List[TopicMeta]) -> Optional[int]:
    for t in existing:
        if t.title.strip().lower() == title.strip().lower():
            return t.id
    new_id = await create_topic(chat_id, title)
    if new_id:
        existing.append(TopicMeta(id=new_id, title=title))
    return new_id

# ══════════════════════════════════════════════════════════════════
#  CHAT INFO
# ══════════════════════════════════════════════════════════════════
async def get_chat_info(chat_id: int) -> Dict:
    info = {"name": "Unknown", "id": chat_id, "type": "Normal",
            "topics": [], "topics_count": 0, "members": 0}
    try:
        chat = await user.get_chat(chat_id)
        info["name"]    = chat.title or getattr(chat, "first_name", "") or "Unknown"
        info["id"]      = chat.id
        info["members"] = chat.members_count or 0
        if getattr(chat, "is_forum", False):
            info["type"] = "Forum"
            topics = await get_forum_topics(chat.id)
            info["topics"]       = [{"id": t.id, "title": t.title,
                                      "icon_color": t.icon_color} for t in topics]
            info["topics_count"] = len(topics)
        elif chat.type == enums.ChatType.CHANNEL:
            info["type"] = "Channel"
    except Exception as e:
        log.error(f"get_chat_info: {e}")
    return info

# ══════════════════════════════════════════════════════════════════
#  MESSAGE FETCHING  — ascending order
# ══════════════════════════════════════════════════════════════════
async def fetch_messages_asc(chat_id: int,
                              topic_id: Optional[int] = None,
                              min_id: int = 0) -> List[Message]:
    collected = []
    offset_id = 0
    while True:
        try:
            chunk = await user.get_messages(chat_id, limit=200, offset_id=offset_id)
        except FloodWait as e:
            await asyncio.sleep(e.value + 2)
            continue
        except Exception as e:
            log.error(f"fetch_messages_asc: {e}")
            break
        if not chunk:
            break
        for msg in chunk:
            if not msg or not msg.id:
                continue
            if msg.id <= min_id:
                continue
            if topic_id is not None:
                rtt = getattr(msg, "reply_to_top_message_id", None)
                rtm = getattr(msg, "reply_to_message_id", None)
                is_root   = (msg.id == topic_id)
                in_thread = (rtt == topic_id) or (rtm == topic_id and rtt is None)
                if not (is_root or in_thread):
                    continue
            collected.append(msg)
        if len(chunk) < 200:
            break
        offset_id = chunk[-1].id
    collected.sort(key=lambda m: m.id)
    return collected

# ══════════════════════════════════════════════════════════════════
#  CLONE ENGINE
# ══════════════════════════════════════════════════════════════════
async def clone_section(
    job: CloneJob, status_msg: Message, uid_key: str,
    src_id: int, dst_id: int,
    topic_info: Optional[Dict],
    dst_topic_id: Optional[int],
) -> Tuple[int, int]:

    topic_name   = topic_info["title"] if topic_info else "General"
    src_topic_id = topic_info["id"]    if topic_info else None
    last_id      = job.last_msg_id.get(topic_name, 0)

    await safe_edit(status_msg, f"📥 Fetching `{topic_name}`…")
    messages = await fetch_messages_asc(src_id, src_topic_id, last_id)

    if not messages:
        job.completed_topics.add(topic_name)
        job.save()
        return 0, 0

    total = len(messages)
    done  = 0
    t0    = time.time()

    for msg in messages:
        if cancel_flags.get(uid_key):
            break
        if msg.service:
            done += 1
            job.last_msg_id[topic_name] = msg.id
            continue

        # build copy kwargs
        copy_kwargs = {"chat_id": dst_id, "disable_notification": True}
        src_reply   = getattr(msg, "reply_to_message_id", None)
        if dst_topic_id and not src_reply:
            copy_kwargs["reply_to_message_id"] = dst_topic_id
        elif src_reply:
            copy_kwargs["reply_to_message_id"] = src_reply

        retries = 4
        success = False
        while retries > 0:
            try:
                await msg.copy(**copy_kwargs)
                success = True
                break
            except FloodWait as e:
                wait = max(e.value, 5)
                await safe_edit(status_msg,
                    f"⏳ FloodWait `{wait}s`…  Topic: `{topic_name}` `{done}/{total}`")
                await asyncio.sleep(wait)
            except (FileReferenceExpired, FileReferenceInvalid):
                retries -= 1
                try:
                    fresh = await user.get_messages(src_id, msg.id)
                    if fresh and fresh.id:
                        msg = fresh
                except Exception:
                    pass
                await asyncio.sleep(1)
            except RPCError as e:
                log.warning(f"RPCError msg {msg.id}: {e}")
                break
            except Exception as e:
                log.error(f"Error msg {msg.id}: {e}")
                break

        if not success:
            job.failed_messages += 1
        else:
            job.cloned_messages += 1

        done += 1
        job.last_msg_id[topic_name] = msg.id

        if done % 10 == 0:
            job.save()
        if done % 5 == 0 or done == total:
            await safe_edit(status_msg, progress_text(job, topic_name, done, total))

        await asyncio.sleep(0.08)

    job.completed_topics.add(topic_name)
    job.save()
    return done, total

# ══════════════════════════════════════════════════════════════════
#  ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════
async def run_clone(uid_key: str, status_msg: Message,
                    src_info: Dict, dst_info: Dict,
                    resume_job: Optional[CloneJob] = None):

    src_id = src_info["id"]
    dst_id = dst_info["id"]

    job = resume_job or CloneJob(
        src_id=src_id, dst_id=dst_id,
        src_name=src_info["name"], dst_name=dst_info["name"],
        src_type=src_info["type"], dst_type=dst_info["type"],
        topics=src_info["topics"], start_time=time.time(),
    )
    job.start_time        = time.time()
    active_jobs[uid_key]  = job
    cancel_flags[uid_key] = False

    topics_to_clone = job.topics if job.topics else [{"id": None, "title": "General"}]

    # pre-count
    await safe_edit(status_msg, "🔢 Counting messages…")
    total_count = 0
    for t in topics_to_clone:
        if t["title"] in job.completed_topics:
            continue
        try:
            msgs = await fetch_messages_asc(
                src_id, t.get("id"),
                job.last_msg_id.get(t["title"], 0)
            )
            total_count += len(msgs)
        except Exception:
            pass
    job.total_messages = total_count
    job.save()

    if total_count == 0:
        await safe_edit(status_msg, "ℹ️ Nothing new to clone.")
        active_jobs.pop(uid_key, None)
        return

    # destination topics cache
    dst_existing: List[TopicMeta] = []
    if dst_info["type"] == "Forum":
        dst_existing = await get_forum_topics(dst_id)

    n = len(topics_to_clone)
    for idx, t in enumerate(topics_to_clone, 1):
        if cancel_flags.get(uid_key):
            break
        if t["title"] in job.completed_topics:
            continue

        dst_topic_id: Optional[int] = None
        if dst_info["type"] == "Forum" and t.get("id"):
            dst_topic_id = await find_or_create_topic(dst_id, t["title"], dst_existing)

        await safe_edit(status_msg,
            f"📌 **[{idx}/{n}]** `{t['title']}`\n"
            f"📂 `{src_info['name']}` ➠ `{dst_info['name']}`"
        )
        await clone_section(job, status_msg, uid_key,
                            src_id, dst_id,
                            t if t.get("id") else None,
                            dst_topic_id)

    elapsed    = time.time() - job.start_time
    cancelled  = cancel_flags.get(uid_key, False)
    icon       = "⏹️ Cancelled" if cancelled else "✅ Clone Complete!"
    summary    = (
        f"{icon}\n\n"
        f"📂 `{job.src_name}` ➠ `{job.dst_name}`\n"
        f"✅ Cloned  : `{job.cloned_messages}` msgs\n"
        f"📌 Topics  : `{len(job.completed_topics)}` / `{n}`\n"
        f"⏰ Time    : `{elapsed_str(elapsed)}`"
    )
    if job.failed_messages:
        summary += f"\n⚠️ Failed   : `{job.failed_messages}` msgs"
    if cancelled:
        summary += "\n\n💾 Progress saved — `/clone` to resume."

    await safe_edit(status_msg, summary)
    active_jobs.pop(uid_key, None)
    cancel_flags.pop(uid_key, None)
    if not cancelled:
        job.delete()

# ══════════════════════════════════════════════════════════════════
#  FORCE-SUB
# ══════════════════════════════════════════════════════════════════
async def check_force_sub(client: Client, uid: int, msg: Message) -> bool:
    if not FORCESUB_CHANNEL:
        return True
    try:
        member = await client.get_chat_member(FORCESUB_CHANNEL, uid)
        if member.status.value in ("banned", "kicked"):
            await msg.reply_text("❌ You are banned.")
            return False
        return True
    except UserNotParticipant:
        try:
            chat = await client.get_chat(FORCESUB_CHANNEL)
            link = chat.invite_link or await client.export_chat_invite_link(FORCESUB_CHANNEL)
        except Exception:
            link = None
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Join Channel", url=link)]]) if link else None
        await msg.reply_text("❌ **Join our channel first!**", reply_markup=kb)
        return False
    except Exception:
        return True

# ══════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════
START_TEXT = (
    "👋 **Universal Cloner Bot**\n"
    "__Dev: Gourav Rajput__\n\n"
    "Clone any Telegram chat with full topic support.\n\n"
    "**Commands:**\n"
    "/clone  — Start a clone job\n"
    "/status — Check progress\n"
    "/cancel — Stop & save progress\n"
    "/help   — Full guide"
)

HELP_TEXT = (
    "📖 **How to use:**\n\n"
    "1️⃣  `/clone`\n"
    "2️⃣  Send **Source** chat\n"
    "3️⃣  Send **Destination** chat\n"
    "4️⃣  Bot handles everything!\n\n"
    "**Accepts:**\n"
    "• `-1001234567890` (numeric ID)\n"
    "• `@username`\n"
    "• `https://t.me/+xxxxxx` (invite link)\n\n"
    "**Works with:**\n"
    "✅ Normal groups\n"
    "✅ Forum topic groups\n"
    "✅ Channels\n"
    "✅ Forward-restricted chats\n"
    "✅ Albums & media\n"
    "✅ Reply chains\n"
    "✅ Resume after cancel\n\n"
    "**Requirements:**\n"
    "• User account = member of source\n"
    "• User account = admin in destination\n\n"
    "__Dev: Gourav Rajput__"
)

async def cmd_start(client: Client, msg: Message):
    if not await check_force_sub(client, msg.from_user.id, msg):
        return
    await msg.reply_text(START_TEXT, reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Help", callback_data="cb_help"),
    ]]))

async def cmd_help(client: Client, msg: Message):
    await msg.reply_text(HELP_TEXT)

async def cmd_status(client: Client, msg: Message):
    k = str(msg.from_user.id)
    job = active_jobs.get(k)
    if not job:
        await msg.reply_text("❌ No active job. Use `/clone` to start.")
        return
    elapsed = time.time() - job.start_time
    await msg.reply_text(
        f"📊 **Clone Running**\n\n"
        f"📂 `{job.src_name}` ➠ `{job.dst_name}`\n"
        f"✅ `{job.cloned_messages}` / `{job.total_messages}` msgs\n"
        f"📌 Topics done: `{len(job.completed_topics)}`\n"
        f"⏰ `{elapsed_str(elapsed)}`"
    )

async def cmd_cancel(client: Client, msg: Message):
    k = str(msg.from_user.id)
    if k not in active_jobs:
        await msg.reply_text("❌ No active clone job.")
        return
    cancel_flags[k] = True
    await msg.reply_text("⏹️ **Cancelling…** progress will be saved.")

async def cmd_clone(client: Client, msg: Message):
    uid = msg.from_user.id
    k   = str(uid)

    if not await check_force_sub(client, uid, msg):
        return
    if k in active_jobs:
        await msg.reply_text("⚠️ Already running! `/cancel` first.")
        return

    # ── source ────────────────────────────────────────────────────
    await msg.reply_text(
        "📤 **Send Source Chat**\n\n"
        "Accepted: numeric ID / @username / invite link\n"
        "_Timeout: 5 min_"
    )
    try:
        src_msg = await client.listen(uid, timeout=300)
    except asyncio.TimeoutError:
        await msg.reply_text("⏰ Timeout. Send `/clone` again.")
        return
    if src_msg.text and src_msg.text.startswith("/"):
        await msg.reply_text("❌ Cancelled.")
        return

    resolving = await src_msg.reply_text("🔍 Resolving source…")
    src_chat  = await resolve_chat(src_msg.text.strip())
    if not src_chat:
        await resolving.edit_text("❌ Source not found. Check ID / username / link.")
        return

    src_info = await get_chat_info(src_chat.id)
    await resolving.edit_text(
        f"✅ **Source:** `{src_info['name']}`\n"
        f"   Type: `{src_info['type']}` | Topics: `{src_info['topics_count']}`\n\n"
        f"📥 **Now send Destination Chat:**"
    )

    # ── destination ───────────────────────────────────────────────
    try:
        dst_msg = await client.listen(uid, timeout=300)
    except asyncio.TimeoutError:
        await msg.reply_text("⏰ Timeout. Send `/clone` again.")
        return
    if dst_msg.text and dst_msg.text.startswith("/"):
        await msg.reply_text("❌ Cancelled.")
        return

    dst_status = await dst_msg.reply_text("🔍 Resolving destination…")
    dst_chat   = await resolve_chat(dst_msg.text.strip())
    if not dst_chat:
        await dst_status.edit_text("❌ Destination not found.")
        return

    dst_info = await get_chat_info(dst_chat.id)
    await dst_status.edit_text(
        f"✅ **Destination:** `{dst_info['name']}`\n"
        f"   Type: `{dst_info['type']}` | Topics: `{dst_info['topics_count']}`"
    )

    # ── resume? ───────────────────────────────────────────────────
    saved    = CloneJob.load(src_info["id"], dst_info["id"])
    resume   = False
    if saved and saved.cloned_messages > 0:
        await dst_status.reply_text(
            f"💾 **Previous progress found!**\n"
            f"Cloned `{saved.cloned_messages}` / `{saved.total_messages}` msgs\n"
            f"Topics: `{len(saved.completed_topics)}`\n\n"
            f"Send `yes` to resume, `no` for fresh start:"
        )
        try:
            ans    = await client.listen(uid, timeout=120)
            resume = ans.text.strip().lower() in ("yes", "y")
        except asyncio.TimeoutError:
            resume = False

    if not resume and saved:
        saved.delete()
        saved = None

    # ── start ─────────────────────────────────────────────────────
    status_msg = await msg.reply_text("🚀 Starting clone…")
    await run_clone(k, status_msg, src_info, dst_info, saved if resume else None)

# ── callback ──────────────────────────────────────────────────────
async def on_callback(client: Client, cb: CallbackQuery):
    await cb.answer()
    if cb.data == "cb_help":
        await cb.message.reply_text(HELP_TEXT)

# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════
async def main():
    global bot, user

    print("=" * 56)
    print("  Universal Cloner Bot  —  Dev: Gourav Rajput")
    print("=" * 56)

    user = Client("user_s", session_string=STRING_SESSION,
                  api_id=API_ID, api_hash=API_HASH, in_memory=True)
    await user.start()
    me = await user.get_me()
    print(f"[User]  {me.first_name}  (@{me.username or 'N/A'})")

    bot = Client("bot_s", bot_token=BOT_TOKEN,
                 api_id=API_ID, api_hash=API_HASH, in_memory=True)

    bot.add_handler(MessageHandler(cmd_start,  filters.command("start")  & filters.private))
    bot.add_handler(MessageHandler(cmd_help,   filters.command("help")   & filters.private))
    bot.add_handler(MessageHandler(cmd_status, filters.command("status") & filters.private))
    bot.add_handler(MessageHandler(cmd_cancel, filters.command("cancel") & filters.private))
    bot.add_handler(MessageHandler(cmd_clone,  filters.command("clone")  & filters.private))
    bot.add_handler(CallbackQueryHandler(on_callback))

    await bot.start()
    bme = await bot.get_me()
    print(f"[Bot ]  @{bme.username}")
    print("\nBot is LIVE! Send /clone to start.\n")

    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as e:
        log.exception(f"Fatal: {e}")
