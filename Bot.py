#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║        UNIVERSAL TELEGRAM CLONER BOT — by Gourav Rajput         ║
║   Clones Groups / Channels / Forums / Topic Groups              ║
║   Fixed: topic threads, media groups, restricted chats         ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import os
import sys
import json
import re
import time
import logging
from typing import Optional, List, Dict, Tuple, Set
from pathlib import Path
from dataclasses import dataclass, field

# ── Fix event loop ───────────────
try:
    _loop = asyncio.get_event_loop()
    if _loop.is_closed():
        raise RuntimeError
except RuntimeError:
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

# ── Pyrogram ─────────────────────
from pyrogram import Client, filters, enums
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message, Chat, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, RPCError, FileReferenceExpired, FileReferenceInvalid
from pyrogram.raw.functions.channels import CreateForumTopic, GetForumTopics
from pyrogram.raw import types as raw_types

# ── pyromod ──────────────────────
from pyromod import listen

# ══════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════
API_ID          = int(os.environ.get("API_ID", 0))
API_HASH        = os.environ.get("API_HASH", "")
BOT_TOKEN       = os.environ.get("BOT_TOKEN", "")
STRING_SESSION  = os.environ.get("STRING_SESSION", "")
OWNER_ID        = int(os.environ.get("OWNER_ID", 0))
FORCESUB_CHANNEL = os.environ.get("FORCESUB_CHANNEL", "")

if not all([API_ID, API_HASH, BOT_TOKEN, STRING_SESSION, OWNER_ID]):
    print("❌ MISSING ENV VARS: API_ID, API_HASH, BOT_TOKEN, STRING_SESSION, OWNER_ID")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger("ClonerBot")

# ══════════════════════════════════════════════════════════════════
#  GLOBALS
# ══════════════════════════════════════════════════════════════════
bot: Client = None
user: Client = None
active_jobs: Dict[int, dict] = {}
cancel_flags: Dict[int, bool] = {}

# ══════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════
def elapsed_str(sec: float) -> str:
    s = int(sec)
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def pbar(done: int, total: int, w=12) -> str:
    f = min(w, int(w * done / total)) if total else 0
    return "█" * f + "░" * (w - f)

# ══════════════════════════════════════════════════════════════════
#  CHAT RESOLUTION
# ══════════════════════════════════════════════════════════════════
async def resolve_chat(identifier: str) -> Optional[Chat]:
    ident = identifier.strip()
    # invite link
    m = re.search(r"(?:joinchat/|\+)([\w\-]+)", ident)
    if m:
        try: return await user.join_chat(m.group(1))
        except: pass
    # t.me link
    m = re.search(r"t\.me/([\w_]+)", ident)
    if m:
        ident = m.group(1)
    # numeric ID
    try: return await user.get_chat(int(ident))
    except ValueError: pass
    # username
    try: return await user.get_chat(ident.lstrip("@"))
    except: return None

# ══════════════════════════════════════════════════════════════════
#  FORUM TOPIC HELPERS
# ══════════════════════════════════════════════════════════════════
async def get_forum_topics(chat_id: int) -> List[dict]:
    topics = []
    try:
        peer = await user.resolve_peer(chat_id)
        offset_id = offset_topic = 0
        while True:
            r = await user.invoke(GetForumTopics(
                peer=peer, q="", offset_date=0,
                offset_id=offset_id, offset_topic=offset_topic, limit=100
            ))
            if not r.topics:
                break
            for t in r.topics:
                if isinstance(t, raw_types.ForumTopic):
                    topics.append({"id": t.id, "title": t.title, "icon_color": getattr(t, "icon_color", 0)})
            if len(r.topics) < 100:
                break
            last = r.topics[-1]
            offset_id = offset_topic = last.id
    except Exception as e:
        log.warning(f"get_forum_topics: {e}")
    return topics

async def create_topic(chat_id: int, title: str) -> Optional[int]:
    try:
        peer = await user.resolve_peer(chat_id)
        r = await user.invoke(CreateForumTopic(
            channel=peer, title=title,
            icon_color=0x6FB9F0, random_id=user.rnd_id(),
        ))
        for upd in r.updates:
            if hasattr(upd, "message") and hasattr(upd.message, "id"):
                return upd.message.id
            if isinstance(upd, raw_types.UpdateMessageID):
                return upd.id
    except Exception as e:
        log.error(f"create_topic '{title}': {e}")
    return None

async def find_or_create_topic(chat_id: int, title: str, existing: List[dict]) -> Optional[int]:
    title_lower = title.strip().lower()
    for t in existing:
        if t["title"].strip().lower() == title_lower:
            return t["id"]
    new_id = await create_topic(chat_id, title)
    if new_id:
        existing.append({"id": new_id, "title": title})
    return new_id

# ══════════════════════════════════════════════════════════════════
#  GET CHAT INFO
# ══════════════════════════════════════════════════════════════════
async def get_chat_info(chat_id: int) -> dict:
    info = {"id": chat_id, "name": "Unknown", "type": "Group", "is_forum": False, "topics": []}
    try:
        chat = await user.get_chat(chat_id)
        info["name"] = chat.title or "Unknown"
        info["is_forum"] = getattr(chat, "is_forum", False)
        info["type"] = "Forum" if info["is_forum"] else ("Channel" if chat.type == enums.ChatType.CHANNEL else "Group")
        if info["is_forum"]:
            info["topics"] = await get_forum_topics(chat.id)
    except Exception as e:
        log.error(f"get_chat_info: {e}")
    return info

# ══════════════════════════════════════════════════════════════════
#  FETCH MESSAGES — with topic filter
# ══════════════════════════════════════════════════════════════════
async def fetch_messages(chat_id: int, min_id: int = 0, 
                          topic_id: Optional[int] = None) -> List[Message]:
    """Fetch messages in ascending order with optional topic filter."""
    collected = []
    offset_id = 0
    while True:
        try:
            chunk = await user.get_messages(chat_id, limit=200, offset_id=offset_id)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            continue
        except Exception as e:
            log.error(f"fetch error: {e}")
            break
        if not chunk:
            break
        for msg in chunk:
            if not msg or not msg.id:
                continue
            if msg.id <= min_id:
                continue
            if topic_id is not None:
                # Topic messages have reply_to_top_message_id == topic_id
                # Root message has id == topic_id
                rtt = getattr(msg, "reply_to_top_message_id", None)
                if not (msg.id == topic_id or rtt == topic_id):
                    continue
            collected.append(msg)
        if len(chunk) < 200:
            break
        offset_id = chunk[-1].id
    collected.sort(key=lambda m: m.id)
    return collected

# ══════════════════════════════════════════════════════════════════
#  COPY SINGLE MESSAGE (handles restricted chats via user account)
# ══════════════════════════════════════════════════════════════════
async def copy_message(msg: Message, dst_id: int, 
                        message_thread_id: Optional[int] = None,
                        reply_to_msg_id: Optional[int] = None) -> bool:
    """Copy a message using the user account (works on restricted chats)."""
    try:
        # For simple text messages
        if msg.text and not msg.media:
            kwargs = {"chat_id": dst_id, "text": msg.text}
            if msg.entities:
                kwargs["entities"] = msg.entities
            if message_thread_id:
                kwargs["message_thread_id"] = message_thread_id
            if reply_to_msg_id and not message_thread_id:
                kwargs["reply_to_message_id"] = reply_to_msg_id
            await user.send_message(**kwargs)
            return True

        # For media — use copy method on user client
        kwargs = {
            "chat_id": dst_id,
            "disable_notification": True,
        }
        if message_thread_id:
            kwargs["message_thread_id"] = message_thread_id
        if reply_to_msg_id and not message_thread_id:
            kwargs["reply_to_message_id"] = reply_to_msg_id

        await msg.copy(**kwargs)
        return True

    except FloodWait as e:
        await asyncio.sleep(e.value + 2)
        return await copy_message(msg, dst_id, message_thread_id, reply_to_msg_id)
    except (FileReferenceExpired, FileReferenceInvalid):
        # Refresh message reference
        try:
            fresh = await user.get_messages(msg.chat.id, msg.id)
            if fresh and fresh.id:
                msg = fresh
                return await copy_message(msg, dst_id, message_thread_id, reply_to_msg_id)
        except:
            pass
        return False
    except RPCError as e:
        log.warning(f"RPCError msg {msg.id}: {e}")
        return False
    except Exception as e:
        log.error(f"copy error msg {msg.id}: {e}")
        return False

# ══════════════════════════════════════════════════════════════════
#  COPY MEDIA GROUP (album)
# ══════════════════════════════════════════════════════════════════
async def copy_media_group_messages(messages: List[Message], dst_id: int,
                                     message_thread_id: Optional[int] = None) -> bool:
    """Copy a media group album using the user account."""
    if not messages:
        return False
    try:
        # Use the first message's ID for the media group
        kwargs = {
            "chat_id": dst_id,
            "from_chat_id": messages[0].chat.id,
            "message_id": messages[0].id,
            "disable_notification": True,
        }
        if message_thread_id:
            kwargs["message_thread_id"] = message_thread_id
        await user.copy_media_group(**kwargs)
        return True
    except Exception as e:
        log.warning(f"copy_media_group failed: {e}")
        # Fallback: copy individually
        success = True
        for msg in messages:
            if not await copy_message(msg, dst_id, message_thread_id=message_thread_id):
                success = False
        return success

# ══════════════════════════════════════════════════════════════════
#  CLONE A TOPIC/SECTION
# ══════════════════════════════════════════════════════════════════
async def clone_section(
    uid: int, status_msg: Message,
    src_id: int, dst_id: int,
    topic_name: str,
    src_topic_id: Optional[int],
    dst_topic_id: Optional[int],
    last_msg_id: int = 0,
) -> Tuple[int, int, int]:
    """Clone messages. Returns (cloned, failed, new_last_id)."""
    
    await safe_edit(status_msg, f"📥 Fetching `{topic_name}`…")
    messages = await fetch_messages(src_id, last_msg_id, src_topic_id)
    
    if not messages:
        return 0, 0, last_msg_id

    # Group media groups together
    media_groups: Dict[str, List[Message]] = {}
    standalone: List[Message] = []
    
    for msg in messages:
        if msg.media_group_id:
            mg_id = str(msg.media_group_id)
            if mg_id not in media_groups:
                media_groups[mg_id] = []
            media_groups[mg_id].append(msg)
        else:
            standalone.append(msg)

    total = len(messages)
    done = 0
    failed = 0
    new_last = last_msg_id
    
    # Determine message_thread_id for destination
    message_thread_id = dst_topic_id if dst_topic_id else None

    # Helper to update status
    async def update_status():
        if done % 5 == 0 or done == total:
            pct = (done / total * 100) if total else 0
            text = (
                f"📂 **Topic:** `{topic_name}`\n"
                f"[`{pbar(done, total)}`] `{pct:.1f}%`\n"
                f"✅ `{done}` / `{total}`"
            )
            await safe_edit(status_msg, text)

    # Process standalone messages
    for msg in standalone:
        if cancel_flags.get(uid):
            break
        if msg.service:
            done += 1
            new_last = max(new_last, msg.id)
            continue

        # Determine reply context
        reply_to_id = None
        rtt = getattr(msg, "reply_to_top_message_id", None)
        rtm = getattr(msg, "reply_to_message_id", None)
        
        # For topic messages: use message_thread_id instead of reply_to
        if message_thread_id:
            reply_to_id = None  # Don't use reply_to, use message_thread_id
        elif rtm:
            reply_to_id = rtm

        success = await copy_message(msg, dst_id, 
                                      message_thread_id=message_thread_id,
                                      reply_to_msg_id=reply_to_id)
        if success:
            done += 1
        else:
            failed += 1
            done += 1
        
        new_last = max(new_last, msg.id)
        await update_status()
        await asyncio.sleep(0.1)  # Rate limiting

    # Process media groups
    for mg_id, msgs in media_groups.items():
        if cancel_flags.get(uid):
            break
        # Sort by message ID
        msgs.sort(key=lambda m: m.id)
        success = await copy_media_group_messages(msgs, dst_id, 
                                                   message_thread_id=message_thread_id)
        if success:
            done += len(msgs)
        else:
            failed += len(msgs)
            done += len(msgs)
        new_last = max(new_last, max(m.id for m in msgs))
        await update_status()
        await asyncio.sleep(0.2)

    return done - failed, failed, new_last

# ══════════════════════════════════════════════════════════════════
#  MAIN CLONE ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════
async def run_clone(uid: int, status_msg: Message,
                     src_id: int, dst_id: int,
                     src_info: dict, dst_info: dict):
    
    job_data = active_jobs.get(uid, {})
    
    # Get source topics
    src_topics = src_info.get("topics", [])
    is_forum_src = src_info.get("is_forum", False)
    is_forum_dst = dst_info.get("is_forum", False)
    
    # Determine what to clone
    if is_forum_src and src_topics:
        # Clone each topic
        topics_to_clone = [(t["title"], t["id"]) for t in src_topics]
    else:
        # Clone general chat
        topics_to_clone = [("General", None)]
    
    # Get destination existing topics (if forum)
    dst_existing_topics = []
    if is_forum_dst:
        dst_existing_topics = await get_forum_topics(dst_id)
    
    total_all = 0
    cloned_all = 0
    failed_all = 0
    start_time = time.time()
    
    # First count total messages
    await safe_edit(status_msg, "🔢 Counting messages…")
    for topic_name, src_topic_id in topics_to_clone:
        last_id = job_data.get(f"last_{topic_name}", 0)
        msgs = await fetch_messages(src_id, last_id, src_topic_id)
        total_all += len(msgs)
    
    if total_all == 0:
        await safe_edit(status_msg, "ℹ️ No new messages to clone.")
        active_jobs.pop(uid, None)
        return
    
    # Clone each topic
    for idx, (topic_name, src_topic_id) in enumerate(topics_to_clone, 1):
        if cancel_flags.get(uid):
            break
        
        last_id = job_data.get(f"last_{topic_name}", 0)
        
        # Find/create destination topic
        dst_topic_id = None
        if is_forum_dst:
            dst_topic_id = await find_or_create_topic(dst_id, topic_name, dst_existing_topics)
        
        await safe_edit(status_msg, 
            f"📌 **[{idx}/{len(topics_to_clone)}]** `{topic_name}`\n"
            f"📂 `{src_info['name']}` ➠ `{dst_info['name']}`")
        
        cloned, failed, new_last = await clone_section(
            uid, status_msg, src_id, dst_id,
            topic_name, src_topic_id, dst_topic_id, last_id
        )
        
        cloned_all += cloned
        failed_all += failed
        job_data[f"last_{topic_name}"] = new_last
        active_jobs[uid] = job_data
    
    elapsed = time.time() - start_time
    cancelled = cancel_flags.get(uid, False)
    
    if cancelled:
        # Save progress as a file for resume
        save_path = Path(f"/tmp/clone_{src_id}_{dst_id}.json")
        save_path.write_text(json.dumps(active_jobs.get(uid, {})))
        summary = (
            f"⏹️ **Cancelled**\n\n"
            f"📂 `{src_info['name']}` ➠ `{dst_info['name']}`\n"
            f"✅ Cloned: `{cloned_all}` / `{total_all}`\n"
            f"📌 Topic: `{topic_name if 'topic_name' in locals() else 'N/A'}`\n"
            f"⏰ `{elapsed_str(elapsed)}`\n\n"
            f"💾 Progress saved — use `/clone` again with same IDs to resume."
        )
    else:
        summary = (
            f"✅ **Clone Complete!**\n\n"
            f"📂 `{src_info['name']}` ➠ `{dst_info['name']}`\n"
            f"✅ Cloned: `{cloned_all}` / `{total_all}`\n"
            f"⚠️ Failed: `{failed_all}`\n"
            f"⏰ Time: `{elapsed_str(elapsed)}`"
        )
    
    await safe_edit(status_msg, summary)
    active_jobs.pop(uid, None)
    cancel_flags.pop(uid, None)

# ══════════════════════════════════════════════════════════════════
#  SAFE EDIT
# ══════════════════════════════════════════════════════════════════
async def safe_edit(msg: Message, text: str):
    try:
        await msg.edit_text(text, disable_web_page_preview=True)
    except:
        pass

# ══════════════════════════════════════════════════════════════════
#  FORCE SUB
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
    except:
        try:
            chat = await client.get_chat(FORCESUB_CHANNEL)
            link = chat.invite_link or await client.export_chat_invite_link(FORCESUB_CHANNEL)
        except:
            link = None
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Join", url=link)]]) if link else None
        await msg.reply_text("❌ **Join our channel first!**", reply_markup=kb)
        return False

# ══════════════════════════════════════════════════════════════════
#  COMMAND: /start
# ══════════════════════════════════════════════════════════════════
async def cmd_start(client: Client, msg: Message):
    if not await check_force_sub(client, msg.from_user.id, msg):
        return
    await msg.reply_text(
        "👋 **Universal Cloner Bot**\n_Dev: Gourav Rajput_\n\n"
        "Clone any Telegram chat with full topic support.\n\n"
        "**Commands:**\n"
        "/clone — Clone messages from source to destination\n"
        "/cancel — Stop current job\n"
        "/help — Full guide"
    )

# ══════════════════════════════════════════════════════════════════
#  COMMAND: /help
# ══════════════════════════════════════════════════════════════════
async def cmd_help(client: Client, msg: Message):
    await msg.reply_text(
        "📖 **How to use /clone:**\n\n"
        "1️⃣ Send `/clone`\n"
        "2️⃣ Send **Source** chat ID/link/username\n"
        "3️⃣ Send **Destination** chat ID/link/username\n"
        "4️⃣ Bot clones everything!\n\n"
        "**Accepted formats:**\n"
        "• `-1001234567890` (numeric ID)\n"
        "• `@username`\n"
        "• `https://t.me/+xxxxxx` (invite link)\n"
        "• `https://t.me/chatname`\n\n"
        "**Supports:**\n"
        "✅ Groups → Groups\n"
        "✅ Channels → Groups\n"
        "✅ Forum topics → Forum topics\n"
        "✅ Group → Forum (creates topics)\n"
        "✅ Restricted/forward-protected chats\n"
        "✅ Media albums\n\n"
        "**Requirements:**\n"
        "• User account = member of source\n"
        "• User account = admin in destination\n\n"
        "**Resume:** If cancelled, just run `/clone` again with same IDs."
    )

# ══════════════════════════════════════════════════════════════════
#  COMMAND: /cancel
# ══════════════════════════════════════════════════════════════════
async def cmd_cancel(client: Client, msg: Message):
    uid = msg.from_user.id
    if uid in active_jobs:
        cancel_flags[uid] = True
        await msg.reply_text("⏹️ **Cancelling…** Progress will be saved for resume.")
    else:
        await msg.reply_text("❌ No active clone job.")

# ══════════════════════════════════════════════════════════════════
#  COMMAND: /clone
# ══════════════════════════════════════════════════════════════════
async def cmd_clone(client: Client, msg: Message):
    uid = msg.from_user.id
    
    if not await check_force_sub(client, uid, msg):
        return
    
    if uid in active_jobs:
        await msg.reply_text("⚠️ Clone already running! Use `/cancel` first.")
        return
    
    # ──────────── GET SOURCE ────────────
    await msg.reply_text(
        "📤 **Send Source Chat**\n\n"
        "Send the source chat ID, @username, or invite link.\n"
        "_Timeout: 5 minutes_"
    )
    
    try:
        src_resp = await client.listen(uid, timeout=300)
    except asyncio.TimeoutError:
        await msg.reply_text("⏰ Timeout. Send `/clone` again.")
        return
    
    if src_resp.text and src_resp.text.startswith("/"):
        await msg.reply_text("❌ Cancelled.")
        return
    
    status = await src_resp.reply_text("🔍 Resolving source…")
    src_chat = await resolve_chat(src_resp.text.strip())
    if not src_chat:
        await status.edit_text("❌ Source not found. Check ID / username / link.")
        return
    
    src_info = await get_chat_info(src_chat.id)
    await status.edit_text(
        f"✅ **Source:** `{src_info['name']}`\n"
        f"   `{src_info['type']}` | Topics: `{len(src_info['topics'])}`\n\n"
        f"📥 **Now send Destination Chat:**"
    )
    
    # ──────────── GET DESTINATION ────────────
    try:
        dst_resp = await client.listen(uid, timeout=300)
    except asyncio.TimeoutError:
        await msg.reply_text("⏰ Timeout. Send `/clone` again.")
        return
    
    if dst_resp.text and dst_resp.text.startswith("/"):
        await msg.reply_text("❌ Cancelled.")
        return
    
    dst_status = await dst_resp.reply_text("🔍 Resolving destination…")
    dst_chat = await resolve_chat(dst_resp.text.strip())
    if not dst_chat:
        await dst_status.edit_text("❌ Destination not found.")
        return
    
    dst_info = await get_chat_info(dst_chat.id)
    await dst_status.edit_text(
        f"✅ **Destination:** `{dst_info['name']}`\n"
        f"   `{dst_info['type']}` | Topics: `{len(dst_info['topics'])}`"
    )
    
    # Check for saved progress
    save_path = Path(f"/tmp/clone_{src_chat.id}_{dst_chat.id}.json")
    if save_path.exists():
        try:
            saved = json.loads(save_path.read_text())
            if saved:
                await dst_status.reply_text(
                    f"💾 **Previous progress found!**\n"
                    f"Send `yes` to resume or `no` for fresh start:"
                )
                try:
                    ans = await client.listen(uid, timeout=120)
                    if ans.text and ans.text.strip().lower() in ("yes", "y"):
                        active_jobs[uid] = saved
                except asyncio.TimeoutError:
                    pass
        except:
            pass
    
    # ──────────── START CLONE ────────────
    status_msg = await msg.reply_text("🚀 Starting clone…")
    active_jobs.setdefault(uid, {})
    cancel_flags[uid] = False
    
    await run_clone(uid, status_msg, src_chat.id, dst_chat.id, src_info, dst_info)
    
    # Clean up saved progress if complete
    if not cancel_flags.get(uid, False) and save_path.exists():
        save_path.unlink()
    elif cancel_flags.get(uid, False):
        pass  # Keep saved progress

# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════
async def main():
    global bot, user
    
    print("=" * 50)
    print("  Universal Cloner Bot — Dev: Gourav Rajput")
    print("=" * 50)
    
    # Start user client
    user = Client("user_s", session_string=STRING_SESSION,
                  api_id=API_ID, api_hash=API_HASH, in_memory=True)
    await user.start()
    me = await user.get_me()
    print(f"✓ User: {me.first_name} (@{me.username or 'N/A'})")
    
    # Start bot
    bot = Client("bot_s", bot_token=BOT_TOKEN,
                 api_id=API_ID, api_hash=API_HASH, in_memory=True)
    
    bot.add_handler(MessageHandler(cmd_start, filters.command("start") & filters.private))
    bot.add_handler(MessageHandler(cmd_help, filters.command("help") & filters.private))
    bot.add_handler(MessageHandler(cmd_cancel, filters.command("cancel") & filters.private))
    bot.add_handler(MessageHandler(cmd_clone, filters.command("clone") & filters.private))
    
    await bot.start()
    bme = await bot.get_me()
    print(f"✓ Bot: @{bme.username}")
    print("\n✓ Bot is LIVE! Send /clone to start.")
    print("=" * 50)
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as e:
        log.exception(f"Fatal: {e}")
