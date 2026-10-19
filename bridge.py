#!/usr/bin/env python3
"""
Secure Telegram-to-Herdr Remote Control Bridge
================================================
Cung cấp cổng điều khiển an toàn qua Telegram Bot để giám sát và tương tác
với các AI Coding Agents đang chạy trong Herdr Workspaces.

Nguyên tắc an toàn cốt lõi:
1. Strict User ID Allowlist: Chỉ những Telegram User ID được cấu hình mới có quyền truy cập.
2. Command Whitelisting: Không cho phép thực thi shell tùy ý.
3. Sanitized Inputs: Kiểm tra và làm sạch toàn bộ dữ liệu đầu vào.
4. Rate Limiting: Chống spam và DoS bot.
5. Audit Logging: Lưu vết toàn bộ hành động người dùng gửi vào log file.
6. Proactive Approval Push: Tự động phát hiện khi agent bị blocked và gửi thông báo kèm nút phê duyệt.
"""

import os
import sys
import json
import time
import re
import logging
import asyncio
from typing import List, Optional, Dict, Any, Tuple, Set
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv

# Nạp các biến môi trường từ file .env nếu có
load_dotenv()

# Thư viện Telegram Bot (python-telegram-bot v20+)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError, RetryAfter, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ------------------------------------------------------------------------------
# Cấu hình & Biến môi trường
# ------------------------------------------------------------------------------
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
AUDIT_LOG_FILE = LOG_DIR / "audit_bridge.log"

# Thiết lập mã hóa UTF-8 cho console Windows để hiển thị trơn tru các emoji
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(AUDIT_LOG_FILE, encoding="utf-8"),
    ]
)
logger = logging.getLogger("HerdrTelegramBridge")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
raw_allowlist = os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "").strip()
ALLOWED_USERS: Set[int] = set()
if raw_allowlist:
    for uid in raw_allowlist.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ALLOWED_USERS.add(int(uid))

RATE_LIMIT_MAX_CALLS = int(os.environ.get("RATE_LIMIT_MAX_CALLS", "10"))
RATE_LIMIT_PERIOD_SEC = int(os.environ.get("RATE_LIMIT_PERIOD_SEC", "60"))

# Cấu hình giám sát tự động
AUTO_NOTIFY_BLOCKED = os.environ.get("AUTO_NOTIFY_BLOCKED", "true").lower() in ("1", "true", "yes")
AUTO_NOTIFY_IDLE = os.environ.get("AUTO_NOTIFY_IDLE", "true").lower() in ("1", "true", "yes")
POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "3"))

NOTIFICATION_CHAT_IDS: Set[int] = set(ALLOWED_USERS)
raw_notify_chats = os.environ.get("NOTIFICATION_CHAT_IDS", "").strip()
if raw_notify_chats:
    for cid in raw_notify_chats.split(","):
        cid = cid.strip()
        if cid.lstrip("-").isdigit():
            NOTIFICATION_CHAT_IDS.add(int(cid))

# Rate limiting storage: user_id -> [timestamps]
user_call_history: Dict[int, List[float]] = defaultdict(list)

# Trạng thái theo dõi các agent đang chạy trong background:
# key: f"{pane_id}:{agent_name}" -> Dict[str, Any]
monitored_agent_states: Dict[str, Dict[str, Any]] = {}

# Lưu thông tin tin nhắn thông báo: message_id -> Dict[str, Any]
notification_message_meta: Dict[int, Dict[str, Any]] = {}

# Cấu hình kết nối mạng Telegram (Network & Timeout Settings)
# Tăng timeout lên 30s để chống lỗi TimedOut khi mạng chập chờn hoặc ISP bóp băng thông quốc tế
TELEGRAM_CONNECT_TIMEOUT = float(os.environ.get("TELEGRAM_CONNECT_TIMEOUT", "30.0"))
TELEGRAM_READ_TIMEOUT = float(os.environ.get("TELEGRAM_READ_TIMEOUT", "30.0"))
TELEGRAM_WRITE_TIMEOUT = float(os.environ.get("TELEGRAM_WRITE_TIMEOUT", "30.0"))
TELEGRAM_POOL_TIMEOUT = float(os.environ.get("TELEGRAM_POOL_TIMEOUT", "15.0"))
TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None

# ------------------------------------------------------------------------------
# Helper & Security Decorators
# ------------------------------------------------------------------------------
def is_rate_limited(user_id: int) -> bool:
    """Kiểm tra giới hạn tần suất gọi lệnh."""
    now = time.time()
    history = user_call_history[user_id]
    valid_calls = [t for t in history if now - t < RATE_LIMIT_PERIOD_SEC]
    user_call_history[user_id] = valid_calls
    if len(valid_calls) >= RATE_LIMIT_MAX_CALLS:
        return True
    user_call_history[user_id].append(now)
    return False

def check_permission(func):
    """Decorator kiểm tra tính hợp lệ của Telegram User ID và Rate Limit."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or user.id not in ALLOWED_USERS:
            uid = user.id if user else "UNKNOWN"
            username = user.username if user else "UNKNOWN"
            logger.warning(f"UNAUTHORIZED ACCESS ATTEMPT | User ID: {uid} | Username: @{username}")
            if update.message:
                await update.message.reply_text("⛔ Quyền truy cập bị từ chối. User ID của bạn chưa nằm trong danh sách Allowlist.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ Không có quyền truy cập.", show_alert=True)
            return

        if is_rate_limited(user.id):
            logger.warning(f"RATE LIMIT EXCEEDED | User ID: {user.id}")
            if update.message:
                await update.message.reply_text("⚠️ Bạn đã gửi lệnh quá nhanh. Vui lòng đợi trong giây lát.")
            elif update.callback_query:
                await update.callback_query.answer("⚠️ Thao tác quá nhanh, hãy đợi.", show_alert=True)
            return

        # Ghi log audit
        cmd_text = update.message.text if update.message else f"Callback: {update.callback_query.data}"
        logger.info(f"AUDIT | User: {user.id} (@{user.username}) | Action: {cmd_text}")
        return await func(update, context, *args, **kwargs)
    return wrapper

# ------------------------------------------------------------------------------
# Herdr CLI Interfacing Layer
# ------------------------------------------------------------------------------
async def run_herdr_cmd(args: List[str]) -> Dict[str, Any]:
    """Thực thi lệnh Herdr CLI một cách an toàn và trả về kết quả."""
    cmd = ["herdr"] + args
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        raw_out = stdout.decode("utf-8", errors="replace").strip()
        raw_err = stderr.decode("utf-8", errors="replace").strip()
        return {
            "exit_code": proc.returncode,
            "stdout": raw_out,
            "stderr": raw_err,
            "success": proc.returncode == 0
        }
    except Exception as e:
        logger.error(f"Lỗi khi thực thi lệnh Herdr {cmd}: {str(e)}")
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": str(e),
            "success": False
        }

async def approve_agent(pane_id: str, agent_name: str) -> Tuple[bool, str]:
    """Phê duyệt yêu cầu của agent bị blocked.
    Gửi 'y' kèm Enter vào terminal pane hoặc gửi Enter vào agent.
    """
    logger.info(f"Đang thực hiện phê duyệt agent `{agent_name}` (Pane `{pane_id}`) ...")
    if pane_id:
        # Gửi chuỗi 'y' rồi phím Enter vào pane terminal
        await run_herdr_cmd(["pane", "send-text", pane_id, "y"])
        r_enter = await run_herdr_cmd(["pane", "send-keys", pane_id, "enter"])
        if r_enter["success"]:
            return True, "Đã gửi xác nhận 'y' + Enter tới terminal pane"

    if agent_name and agent_name != "Unknown":
        r_keys = await run_herdr_cmd(["agent", "send-keys", agent_name, "enter"])
        if r_keys["success"]:
            return True, "Đã gửi phím Enter tới agent"
        r_prompt = await run_herdr_cmd(["agent", "prompt", agent_name, "y"])
        if r_prompt["success"]:
            return True, "Đã gửi prompt 'y' tới agent"

    return False, "Không thể gửi phím phê duyệt tới pane hoặc agent"

async def reject_agent(pane_id: str, agent_name: str) -> Tuple[bool, str]:
    """Từ chối yêu cầu của agent bị blocked.
    Gửi 'n' kèm Enter hoặc Esc / Ctrl+C vào pane terminal.
    """
    logger.info(f"Đang thực hiện từ chối agent `{agent_name}` (Pane `{pane_id}`) ...")
    if pane_id:
        await run_herdr_cmd(["pane", "send-text", pane_id, "n"])
        r_enter = await run_herdr_cmd(["pane", "send-keys", pane_id, "enter"])
        if r_enter["success"]:
            return True, "Đã gửi từ chối 'n' + Enter tới terminal pane"
        r_esc = await run_herdr_cmd(["pane", "send-keys", pane_id, "esc"])
        if r_esc["success"]:
            return True, "Đã gửi phím Escape tới terminal pane"

    if agent_name and agent_name != "Unknown":
        r_esc = await run_herdr_cmd(["agent", "send-keys", agent_name, "esc"])
        if r_esc["success"]:
            return True, "Đã gửi phím Escape tới agent"
        r_prompt = await run_herdr_cmd(["agent", "prompt", agent_name, "n"])
        if r_prompt["success"]:
            return True, "Đã gửi prompt 'n' tới agent"

    return False, "Không thể gửi tín hiệu từ chối tới pane hoặc agent"

async def resolve_valid_pane_id(pane_id: str) -> str:
    """Đảm bảo pane_id hợp lệ (có dạng 'wX:pY').
    Nếu người dùng, regex hoặc callback vô tình nhận vào workspace_id (ví dụ 'w2'),
    hàm sẽ tự động tra cứu để tìm pane_id chính xác (ví dụ 'w2:p1').
    """
    if not pane_id:
        return ""
    if ":" in pane_id:
        return pane_id

    # 1. Tra cứu trong các agent đang theo dõi
    for info in monitored_agent_states.values():
        p_id = info.get("pane_id", "")
        ws_id = info.get("workspace_id", "")
        if p_id.startswith(f"{pane_id}:") or ws_id == pane_id:
            logger.info(f"Tự động chuẩn hóa Pane ID từ '{pane_id}' thành '{p_id}' từ monitored states")
            return p_id

    # 2. Tra cứu qua herdr agent list
    res = await run_herdr_cmd(["agent", "list"])
    if res["success"] and res["stdout"]:
        try:
            data = json.loads(res["stdout"])
            for ag in data.get("result", {}).get("agents", []):
                p_id = ag.get("pane_id", "")
                ws_id = ag.get("workspace_id", "")
                if p_id.startswith(f"{pane_id}:") or ws_id == pane_id:
                    logger.info(f"Tự động chuẩn hóa Pane ID từ '{pane_id}' thành '{p_id}' qua herdr agent list")
                    return p_id
        except Exception:
            pass

    # 3. Tra cứu qua herdr pane list
    res_pane = await run_herdr_cmd(["pane", "list"])
    if res_pane["success"] and res_pane["stdout"]:
        try:
            data = json.loads(res_pane["stdout"])
            for p in data.get("result", {}).get("panes", []):
                p_id = p.get("pane_id", "")
                if p_id.startswith(f"{pane_id}:"):
                    logger.info(f"Tự động chuẩn hóa Pane ID từ '{pane_id}' thành '{p_id}' qua herdr pane list")
                    return p_id
        except Exception:
            pass

    return pane_id

def parse_callback_data(data: str) -> Tuple[str, str, str]:
    """Phân tách callback data an toàn tuyệt đối kể cả khi pane_id chứa dấu hai chấm ':' (ví dụ 'w2:p1').
    Hỗ trợ cả định dạng mới '|' và định dạng cũ ':'.
    """
    if "|" in data:
        parts = data.split("|")
        prefix = parts[0]
        pane_id = parts[1] if len(parts) > 1 else ""
        extra = parts[2] if len(parts) > 2 else ""
        return prefix, pane_id, extra

    # Hỗ trợ định dạng cũ có dấu ':'
    if data.startswith("logs_"):
        return "logs", data.replace("logs_", ""), ""

    parts = data.split(":")
    if len(parts) >= 4:
        # Ví dụ: ['k', 'w2', 'p1', 'enter'] hoặc ['mode', 'w2', 'p1', 'tui']
        prefix = parts[0]
        pane_id = f"{parts[1]}:{parts[2]}"
        extra = ":".join(parts[3:])
        return prefix, pane_id, extra
    elif len(parts) == 3:
        prefix = parts[0]
        return prefix, parts[1], parts[2]
    elif len(parts) == 2:
        return parts[0], parts[1], ""

    return data, "", ""

# ------------------------------------------------------------------------------
# Visual Formatting & UI Helpers
# ------------------------------------------------------------------------------
ANSI_REGEX = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def strip_ansi(text: str) -> str:
    """Loại bỏ các mã màu ANSI terminal để text log sạch sẽ."""
    return ANSI_REGEX.sub('', text) if text else ""

def detect_terminal_choices(text: str) -> Tuple[List[Dict[str, str]], bool]:
    """Phân tích nội dung terminal để tự động phát hiện các lựa chọn của Agent.
    Hỗ trợ:
    - Danh sách đánh số: 1. Option, 1) Option, [1] Option, (1) Option, ❯ 1) Option
    - Phím tắt chữ cái: (y)es, (n)o, (a)lways, (d)iff, (e)dit
    - Định dạng slash: [y/n/a/d/q]
    - Phát hiện giao diện tương tác TUI (dấu ❯, ›, arrow keys, etc.)
    """
    choices = []
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    
    # Kiểm tra xem có dấu hiệu của interactive TUI menu không
    is_tui = any(
        any(cursor in line for cursor in ("❯", "›", "●", "->", "=>")) or
        any(kw in line.lower() for kw in ("arrow keys", "select an option", "choose one", "press enter", "select option"))
        for line in lines
    )
    
    # 1. Quét các lựa chọn đánh số: 1) hoặc 1. hoặc [1] hoặc (1) hoặc ❯ 1)
    num_pattern = re.compile(r'^(?:[❯>•\-\*]\s*)?(?:\[(\d+)\]|\((\d+)\)|(\d+)[\.\:\)])\s*(.+)$')
    for line in lines:
        m = num_pattern.match(line)
        if m:
            val = m.group(1) or m.group(2) or m.group(3)
            raw_label = m.group(4).strip()
            # Bỏ qua nếu label quá dài (có thể là code/log ngẫu nhiên) hoặc quá ngắn
            if 1 <= len(raw_label) <= 80:
                short_label = raw_label[:20] + "…" if len(raw_label) > 20 else raw_label
                emoji_digits = {'1':'1️⃣','2':'2️⃣','3':'3️⃣','4':'4️⃣','5':'5️⃣','6':'6️⃣','7':'7️⃣','8':'8️⃣','9':'9️⃣'}
                emoji = emoji_digits.get(val, f"[{val}]")
                choices.append({
                    "key": val,
                    "label": f"{emoji} {short_label}",
                    "raw": raw_label
                })

    # 2. Nếu không có số, kiểm tra các phím tắt chữ cái kiểu (y)es, (n)o, (a)lways, (d)iff, (e)dit
    if not choices:
        letter_pattern = re.compile(r'\(([a-zA-Z0-9])\)\s*([a-zA-Z0-9_\-]{1,15})')
        for line in lines:
            matches = letter_pattern.findall(line)
            if len(matches) >= 2:
                for key, word in matches:
                    full_word = f"{key}{word}".capitalize()
                    choices.append({
                        "key": key,
                        "label": f"[{key.upper()}] {full_word}",
                        "raw": f"({key}) {word}"
                    })
                break

    # 3. Kiểm tra định dạng slash kiểu [y/n/a/d/q]
    if not choices:
        slash_match = re.search(r'\[([a-zA-Z]/[a-zA-Z/]+)\]', text)
        if slash_match:
            parts = slash_match.group(1).split('/')
            for p in parts:
                p = p.strip()
                if p:
                    choices.append({
                        "key": p,
                        "label": f"[{p.upper()}]",
                        "raw": p
                    })

    return choices[:6], is_tui

def build_blocked_message_text(
    agent_name: str,
    pane_id: str,
    workspace_id: str,
    status_raw: str,
    terminal_snippet: str,
    mode: str = "main"
) -> str:
    """Tạo nội dung tin nhắn Markdown hiển thị thông báo chờ duyệt hoặc điều khiển TUI."""
    safe_snippet = terminal_snippet.replace("```", "'''")
    
    if mode == "tui":
        return (
            "🎮 *ĐIỀU KHIỂN BÀN PHÍM TUI CHO AGENT*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 *Agent:* `{agent_name}` | 📍 Pane `{pane_id}`\n\n"
            f"📋 *Màn hình terminal hiện tại:*\n"
            f"```text\n{safe_snippet}\n```\n"
            "👉 _Dùng phím `⬆️ Lên`, `⬇️ Xuống` để di chuyển con trỏ, `␣ Space` để đánh dấu, và `⏎ Enter` để chọn._"
        )
    elif mode == "num":
        return (
            "🔢 *CHỌN NHANH THEO SỐ CHO AGENT*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 *Agent:* `{agent_name}` | 📍 Pane `{pane_id}`\n\n"
            f"📋 *Nội dung terminal gần nhất:*\n"
            f"```text\n{safe_snippet}\n```\n"
            "👉 _Bấm một trong các số bên dưới để gửi số + phím Enter vào terminal._"
        )
    else:
        return (
            "🚨 *YÊU CẦU PHÊ DUYỆT / LỰA CHỌN TỪ AI AGENT!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 *Agent:* `{agent_name}`\n"
            f"📍 *Vị trí:* Pane `{pane_id}`" + (f" (Workspace `{workspace_id}`)" if workspace_id else "") + "\n"
            f"⚠️ *Trạng thái:* {format_agent_status_badge(status_raw)}\n\n"
            f"📋 *Nội dung terminal gần nhất:*\n"
            f"```text\n{safe_snippet}\n```\n"
            "👉 _Bấm lựa chọn bên dưới, mở Bàn phím TUI, hoặc **Reply** trực tiếp tin nhắn này để nhập tự do._"
        )

def build_idle_message_text(
    agent_name: str,
    pane_id: str,
    workspace_id: str,
    terminal_snippet: str,
    cwd: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """Tạo nội dung tin nhắn Markdown thông báo khi agent hoàn thành công việc và chuyển sang rảnh rỗi (Idle)."""
    safe_snippet = terminal_snippet.replace("```", "'''")
    cwd_name = Path(cwd).name if cwd else ""
    loc_parts = []
    if pane_id:
        loc_parts.append(f"Pane `{pane_id}`")
    if workspace_id:
        loc_parts.append(f"Workspace `{workspace_id}`")
    if cwd_name:
        loc_parts.append(f"Thư mục `{cwd_name}`")
    loc_str = " | ".join(loc_parts)

    safe_title = title.replace("`", "'") if title else ""
    title_line = f"▫️ *Tiêu đề/Tác vụ:* `{safe_title}`\n" if safe_title and safe_title != agent_name else ""

    return (
        "🎉 *AI AGENT ĐÃ HOÀN THÀNH CÔNG VIỆC!*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 *Agent:* `{agent_name}`\n"
        f"📍 *Vị trí:* {loc_str}\n"
        f"{title_line}"
        f"⚡ *Trạng thái:* 🔵 Rảnh rỗi (Idle) — Sẵn sàng nhận việc\n\n"
        f"📋 *Kết quả / Output gần nhất:*\n"
        f"```text\n{safe_snippet}\n```\n"
        "👉 _Bạn có thể **Reply** trực tiếp tin nhắn này để giao nhiệm vụ tiếp theo, hoặc bấm nút bên dưới._"
    )

def build_idle_keyboard(pane_id: str, agent_name: str) -> InlineKeyboardMarkup:
    """Tạo inline keyboard cho thông báo agent hoàn thành công việc."""
    keyboard = [
        [
            InlineKeyboardButton("💬 Giao nhiệm vụ tiếp theo", callback_data=f"prompt_hint|{pane_id}|{agent_name}"),
            InlineKeyboardButton("📜 Xem 30 dòng Log", callback_data=f"logs|{pane_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Trạng thái Agents", callback_data="refresh_agents||"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_approval_keyboard(
    pane_id: str,
    agent_name: str,
    terminal_text: str,
    mode: str = "main"
) -> InlineKeyboardMarkup:
    """Tạo inline keyboard đa năng cho phép phê duyệt, chọn option tự động hoặc điều khiển TUI."""
    keyboard: List[List[InlineKeyboardButton]] = []

    if mode == "tui":
        keyboard.append([
            InlineKeyboardButton("⬆️ Lên", callback_data=f"k|{pane_id}|up"),
            InlineKeyboardButton("⏎ Enter (Chọn)", callback_data=f"k|{pane_id}|enter"),
            InlineKeyboardButton("⬇️ Xuống", callback_data=f"k|{pane_id}|down"),
        ])
        keyboard.append([
            InlineKeyboardButton("␣ Space", callback_data=f"k|{pane_id}|space"),
            InlineKeyboardButton("⎋ Esc (Hủy)", callback_data=f"k|{pane_id}|esc"),
            InlineKeyboardButton("🔄 Cập nhật", callback_data=f"k|{pane_id}|refresh"),
        ])
        keyboard.append([
            InlineKeyboardButton("🔙 Về menu chính", callback_data=f"mode|{pane_id}|main"),
        ])
        return InlineKeyboardMarkup(keyboard)

    elif mode == "num":
        keyboard.append([
            InlineKeyboardButton("1️⃣", callback_data=f"c|{pane_id}|1"),
            InlineKeyboardButton("2️⃣", callback_data=f"c|{pane_id}|2"),
            InlineKeyboardButton("3️⃣", callback_data=f"c|{pane_id}|3"),
            InlineKeyboardButton("4️⃣", callback_data=f"c|{pane_id}|4"),
            InlineKeyboardButton("5️⃣", callback_data=f"c|{pane_id}|5"),
        ])
        keyboard.append([
            InlineKeyboardButton("🔙 Về menu chính", callback_data=f"mode|{pane_id}|main"),
        ])
        return InlineKeyboardMarkup(keyboard)

    # Chế độ "main":
    choices, is_tui = detect_terminal_choices(terminal_text)
    if choices:
        row = []
        for ch in choices:
            btn = InlineKeyboardButton(ch["label"], callback_data=f"c|{pane_id}|{ch['key']}")
            row.append(btn)
            if len(row) == 2 or len(ch["label"]) > 16:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

    # Hàng duyệt / từ chối chuẩn
    keyboard.append([
        InlineKeyboardButton("✅ Phê duyệt (y)", callback_data=f"autoapprove|{pane_id}|{agent_name}"),
        InlineKeyboardButton("❌ Từ chối (n)", callback_data=f"autoreject|{pane_id}|{agent_name}"),
    ])

    # Hàng điều khiển nâng cao (TUI / Số)
    control_row = [
        InlineKeyboardButton("🎮 Bàn phím TUI (⬆️⬇️⏎)", callback_data=f"mode|{pane_id}|tui"),
        InlineKeyboardButton("🔢 Phím số (1-5)", callback_data=f"mode|{pane_id}|num"),
    ]
    keyboard.append(control_row)

    # Hàng tiện ích
    keyboard.append([
        InlineKeyboardButton("📜 Xem 30 dòng Log", callback_data=f"logs|{pane_id}"),
        InlineKeyboardButton("🛑 Dừng (Ctrl+C)", callback_data=f"autostop|{pane_id}|{agent_name}"),
    ])

    return InlineKeyboardMarkup(keyboard)

def format_agent_status_badge(status: Optional[str]) -> str:
    """Tạo badge trạng thái kèm biểu tượng trực quan."""
    s = (status or "").lower()
    if any(k in s for k in ("working", "running", "active", "busy")):
        return "🟢 Đang làm việc (Working) ⚡"
    elif any(k in s for k in ("blocked", "waiting", "prompt", "question", "approval")):
        return "🟡 Chờ duyệt (Blocked) ⚠️"
    elif any(k in s for k in ("idle", "ready", "done", "completed")):
        return "🔵 Rảnh rỗi (Idle) 💤"
    elif any(k in s for k in ("error", "fail", "crashed")):
        return "🔴 Lỗi (Error) ❌"
    elif any(k in s for k in ("unknown", "none")) or not s:
        return "⚪ Không xác định"
    return f"ℹ️ {status}"

def format_status_card(res_json: Dict[str, Any], raw_fallback: str) -> str:
    """Định dạng trạng thái Herdr System thành giao diện thẻ trực quan."""
    try:
        server = res_json.get("server", {})
        client = res_json.get("client", {})
        is_running = server.get("running", False) or server.get("status") == "running"

        server_badge = "🟢 ONLINE (Đang chạy)" if is_running else "🔴 OFFLINE (Chưa chạy)"
        srv_ver = server.get("version", "N/A")
        srv_proto = server.get("protocol", "N/A")
        socket_path = server.get("socket", "N/A")

        cli_ver = client.get("version", "N/A")
        channel = client.get("channel", "stable")

        return (
            "🖥️ *HERDR SYSTEM STATUS*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"• *Server:* {server_badge}\n"
            f"   ▫️ Phiên bản: `{srv_ver}` (Protocol: `{srv_proto}`)\n"
            f"   ▫️ Socket: `{socket_path}`\n\n"
            f"• *Client CLI:* 📱 Sẵn sàng\n"
            f"   ▫️ Phiên bản: `{cli_ver}` (Kênh: `{channel}`)\n"
        )
    except Exception as e:
        logger.error(f"Lỗi format status card: {e}")
        return f"🖥️ *Herdr Status:*\n```text\n{raw_fallback}\n```"

def format_workspaces_card(raw_stdout: str) -> str:
    """Định dạng danh sách Workspaces thành giao diện thẻ trực quan."""
    try:
        data = json.loads(raw_stdout)
        workspaces = data.get("result", {}).get("workspaces", [])
        if not workspaces:
            return (
                "📂 *DANH SÁCH WORKSPACES*\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "ℹ️ _Hiện không có workspace nào đang mở._"
            )

        cards = [f"📂 *DANH SÁCH WORKSPACES* ({len(workspaces)})\n━━━━━━━━━━━━━━━━━━"]
        for ws in workspaces:
            label = ws.get("label") or "Không tên"
            ws_id = ws.get("workspace_id", "")
            number = ws.get("number", "")
            focused = ws.get("focused", False)
            focus_str = " 🎯 *(Đang chọn)*" if focused else ""
            status_badge = format_agent_status_badge(ws.get("agent_status"))
            tab_count = ws.get("tab_count", 0)
            pane_count = ws.get("pane_count", 0)
            active_tab = ws.get("active_tab_id", "N/A")

            cards.append(
                f"🔹 *#{number} • {label}* (`{ws_id}`){focus_str}\n"
                f"   ▫️ *Trạng thái Agent:* {status_badge}\n"
                f"   ▫️ *Quy mô:* `{tab_count}` Tab | `{pane_count}` Pane\n"
                f"   ▫️ *Tab hiện tại:* `{active_tab}`"
            )
        return "\n\n".join(cards)
    except Exception as e:
        logger.error(f"Lỗi format workspaces card: {e}")
        return f"📂 *Danh sách Herdr Workspaces:*\n```text\n{raw_stdout}\n```"

def format_agents_card(raw_stdout: str) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """Định dạng danh sách AI Agents thành giao diện thẻ và tạo action buttons."""
    try:
        data = json.loads(raw_stdout)
        agents = data.get("result", {}).get("agents", [])
        if not agents:
            return (
                "🤖 *DANH SÁCH AI AGENTS*\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "ℹ️ _Hiện không có AI Agent nào đang chạy trong các Panes._\n\n"
                "💡 _Gợi ý:_ Dùng `/workspaces` để kiểm tra các không gian làm việc đang mở.",
                None
            )

        cards = [f"🤖 *DANH SÁCH AI AGENTS* ({len(agents)})\n━━━━━━━━━━━━━━━━━━"]
        keyboard = []

        for ag in agents:
            name = ag.get("agent") or ag.get("name") or ag.get("agent_id") or ag.get("id") or "Unknown"
            kind = ag.get("agent_session", {}).get("agent") or ag.get("kind") or ag.get("type") or "Agent"
            pane_id = ag.get("pane_id", "")
            status_raw = ag.get("agent_status") or ag.get("status") or ag.get("lifecycle_state") or ag.get("state") or "unknown"
            status_badge = format_agent_status_badge(status_raw)
            ws_id = ag.get("workspace_id", "")

            loc_str = f"`{pane_id}`" if pane_id else f"`{ws_id}`"
            cards.append(
                f"👤 *Agent:* `{name}` `[{kind}]`\n"
                f"   ▫️ *Vị trí:* Pane {loc_str}\n"
                f"   ▫️ *Trạng thái:* {status_badge}"
            )

            row_buttons = []
            if any(k in status_raw.lower() for k in ("blocked", "wait", "approval", "question")):
                row_buttons.extend([
                    InlineKeyboardButton(f"✅ Duyệt {name}", callback_data=f"autoapprove|{pane_id}|{name}"),
                    InlineKeyboardButton(f"❌ Từ chối", callback_data=f"autoreject|{pane_id}|{name}"),
                    InlineKeyboardButton(f"🎮 Điều khiển", callback_data=f"mode|{pane_id}|main")
                ])
            if pane_id:
                row_buttons.append(
                    InlineKeyboardButton(f"📜 Log {name}", callback_data=f"logs|{pane_id}")
                )
            if row_buttons:
                keyboard.append(row_buttons)

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        return "\n\n".join(cards), reply_markup
    except Exception as e:
        logger.error(f"Lỗi format agents card: {e}")
        return f"🤖 *Danh sách AI Agents:*\n```text\n{raw_stdout}\n```", None

# ------------------------------------------------------------------------------
# Proactive Background Monitor (Tự động gửi thông báo phê duyệt & hoàn thành)
# ------------------------------------------------------------------------------
async def monitor_agents_job(context: ContextTypes.DEFAULT_TYPE):
    """Job nền chạy lặp lại định kỳ để giám sát agent:
    1. Phát hiện agent bị Blocked và tự động gửi thông báo duyệt kèm bàn phím tương tác.
    2. Phát hiện agent chuyển trạng thái từ Working sang Idle (hoàn thành) và tự động gửi thông báo.
    """
    if not (AUTO_NOTIFY_BLOCKED or AUTO_NOTIFY_IDLE) or not NOTIFICATION_CHAT_IDS:
        return

    res = await run_herdr_cmd(["agent", "list"])
    if not res["success"] or not res["stdout"]:
        return

    try:
        data = json.loads(res["stdout"])
        agents = data.get("result", {}).get("agents", [])
    except Exception as e:
        logger.debug(f"Không thể parse JSON herdr agent list: {e}")
        return

    active_keys = set()
    for ag in agents:
        agent_name = ag.get("agent") or ag.get("name") or ag.get("agent_id") or ag.get("id") or "Unknown"
        pane_id = ag.get("pane_id", "")
        workspace_id = ag.get("workspace_id", "")
        status_raw = ag.get("agent_status") or ag.get("status") or ag.get("lifecycle_state") or ag.get("state") or "unknown"
        status = status_raw.lower()
        seq = ag.get("state_change_seq", 0)
        completion_seq = ag.get("completion_seq")
        terminal_title = ag.get("terminal_title_stripped") or ag.get("terminal_title") or ""
        cwd = ag.get("cwd") or ""
        key = f"{pane_id}:{agent_name}"
        active_keys.add(key)

        is_blocked = any(k in status for k in ("blocked", "wait", "question", "approval"))
        is_working = any(k in status for k in ("working", "running", "active", "busy"))
        is_idle = any(k in status for k in ("idle", "ready", "done", "completed"))

        prev = monitored_agent_states.get(key)

        # ----------------------------------------------------------------------
        # TRƯỜNG HỢP 1: Agent mới xuất hiện lần đầu (hoặc bot vừa khởi động)
        # ----------------------------------------------------------------------
        if prev is None:
            monitored_agent_states[key] = {
                "seq": seq,
                "completion_seq": completion_seq,
                "is_blocked": is_blocked,
                "was_working": is_working,
                "status": status,
                "status_raw": status_raw,
                "pane_id": pane_id,
                "agent_name": agent_name,
                "workspace_id": workspace_id,
                "cwd": cwd,
                "terminal_title": terminal_title,
                "messages": [],
                "last_idle_notified_seq": seq if is_idle else None,
                "last_blocked_notified_seq": None,
                "notified_at": 0,
            }

            # Nếu agent đang bị blocked ngay lúc khởi động, gửi thông báo phê duyệt
            if is_blocked and AUTO_NOTIFY_BLOCKED:
                logger.info(f"Phát hiện Agent `{agent_name}` (Pane `{pane_id}`) đang ở trạng thái BLOCKED lúc khởi động. Đang gửi thông báo duyệt...")
                terminal_snippet = "(Không có nội dung hiển thị trong terminal)"
                if pane_id:
                    read_res = await run_herdr_cmd(["pane", "read", pane_id, "--lines", "15"])
                    if read_res["success"]:
                        cleaned = strip_ansi(read_res["stdout"]).strip()
                        if cleaned:
                            terminal_snippet = cleaned[-1500:]

                msg_text = build_blocked_message_text(agent_name, pane_id, workspace_id, status_raw, terminal_snippet, mode="main")
                reply_markup = build_approval_keyboard(pane_id, agent_name, terminal_snippet, mode="main")

                sent_records = []
                for chat_id in NOTIFICATION_CHAT_IDS:
                    try:
                        sent_msg = await context.bot.send_message(
                            chat_id=chat_id,
                            text=msg_text,
                            parse_mode="Markdown",
                            reply_markup=reply_markup
                        )
                        sent_records.append({"chat_id": chat_id, "message_id": sent_msg.message_id})
                        notification_message_meta[sent_msg.message_id] = {
                            "pane_id": pane_id,
                            "agent_name": agent_name,
                            "workspace_id": workspace_id,
                            "status_raw": status_raw,
                            "terminal_snippet": terminal_snippet,
                            "type": "blocked_notification",
                        }
                    except Exception as e:
                        logger.error(f"Lỗi gửi thông báo tự động tới chat {chat_id}: {e}")

                monitored_agent_states[key]["is_blocked"] = True
                monitored_agent_states[key]["last_blocked_notified_seq"] = seq
                monitored_agent_states[key]["messages"] = sent_records
                monitored_agent_states[key]["notified_at"] = time.time()
            continue

        # ----------------------------------------------------------------------
        # TRƯỜNG HỢP 2: Agent đang theo dõi
        # ----------------------------------------------------------------------

        # 2A. Agent chuyển sang hoặc đang ở trạng thái BLOCKED (Chờ duyệt / lựa chọn)
        if is_blocked and AUTO_NOTIFY_BLOCKED:
            if not prev.get("is_blocked") or prev.get("last_blocked_notified_seq") != seq:
                logger.info(f"Phát hiện Agent `{agent_name}` (Pane `{pane_id}`) chuyển sang trạng thái BLOCKED! Đang gửi thông báo duyệt...")

                terminal_snippet = "(Không có nội dung hiển thị trong terminal)"
                if pane_id:
                    read_res = await run_herdr_cmd(["pane", "read", pane_id, "--lines", "15"])
                    if read_res["success"]:
                        cleaned = strip_ansi(read_res["stdout"]).strip()
                        if cleaned:
                            terminal_snippet = cleaned[-1500:]

                msg_text = build_blocked_message_text(agent_name, pane_id, workspace_id, status_raw, terminal_snippet, mode="main")
                reply_markup = build_approval_keyboard(pane_id, agent_name, terminal_snippet, mode="main")

                sent_records = []
                for chat_id in NOTIFICATION_CHAT_IDS:
                    try:
                        sent_msg = await context.bot.send_message(
                            chat_id=chat_id,
                            text=msg_text,
                            parse_mode="Markdown",
                            reply_markup=reply_markup
                        )
                        sent_records.append({"chat_id": chat_id, "message_id": sent_msg.message_id})
                        notification_message_meta[sent_msg.message_id] = {
                            "pane_id": pane_id,
                            "agent_name": agent_name,
                            "workspace_id": workspace_id,
                            "status_raw": status_raw,
                            "terminal_snippet": terminal_snippet,
                            "type": "blocked_notification",
                        }
                    except Exception as e:
                        logger.error(f"Lỗi gửi thông báo tự động tới chat {chat_id}: {e}")

                prev["is_blocked"] = True
                prev["was_working"] = True
                prev["last_blocked_notified_seq"] = seq
                prev["status"] = status
                prev["status_raw"] = status_raw
                prev["seq"] = seq
                prev["messages"] = sent_records
                prev["terminal_snippet"] = terminal_snippet
                prev["notified_at"] = time.time()

        # 2B. Agent đang làm việc (WORKING)
        elif is_working:
            if prev.get("is_blocked"):
                logger.info(f"Agent `{agent_name}` (Pane `{pane_id}`) đã chuyển trạng thái từ blocked sang: {status_raw}. Cập nhật tin nhắn Telegram...")
                for msg_rec in prev.get("messages", []):
                    try:
                        await context.bot.edit_message_text(
                            chat_id=msg_rec["chat_id"],
                            message_id=msg_rec["message_id"],
                            text=(
                                f"ℹ️ *Agent `{agent_name}` (Pane `{pane_id}`)* đã tiếp tục hoạt động.\n"
                                f"Trạng thái hiện tại: {format_agent_status_badge(status_raw)}"
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                prev["is_blocked"] = False
                prev["messages"] = []

            # Đánh dấu agent đang làm việc
            prev["was_working"] = True
            prev["status"] = status
            prev["status_raw"] = status_raw
            prev["seq"] = seq
            prev["cwd"] = cwd
            prev["terminal_title"] = terminal_title

        # 2C. Agent chuyển sang hoặc đang ở trạng thái rảnh rỗi / hoàn thành (IDLE / DONE)
        else:
            if prev.get("is_blocked"):
                logger.info(f"Agent `{agent_name}` (Pane `{pane_id}`) đã kết thúc từ trạng thái blocked sang: {status_raw}.")
                for msg_rec in prev.get("messages", []):
                    try:
                        await context.bot.edit_message_text(
                            chat_id=msg_rec["chat_id"],
                            message_id=msg_rec["message_id"],
                            text=(
                                f"ℹ️ *Agent `{agent_name}` (Pane `{pane_id}`)* đã kết thúc lượt xử lý.\n"
                                f"Trạng thái hiện tại: {format_agent_status_badge(status_raw)}"
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                prev["is_blocked"] = False
                prev["messages"] = []

            # Kiểm tra chuyển tiếp từ WORKING sang IDLE
            should_notify_idle = False
            if AUTO_NOTIFY_IDLE and is_idle:
                prev_was_working = prev.get("was_working", False)
                prev_status = prev.get("status", "")
                prev_completion_seq = prev.get("completion_seq")
                new_completion = (completion_seq is not None and completion_seq != prev_completion_seq)
                is_transition = prev_was_working or new_completion or any(k in prev_status for k in ("working", "running", "active", "busy"))
                is_new_seq = prev.get("last_idle_notified_seq") != seq

                if is_transition and is_new_seq:
                    should_notify_idle = True

            if should_notify_idle:
                logger.info(f"🎉 Phát hiện Agent `{agent_name}` (Pane `{pane_id}`) HOÀN THÀNH CÔNG VIỆC (chuyển sang IDLE)! Gửi thông báo...")

                terminal_snippet = "(Không có nội dung hiển thị trong terminal)"
                if pane_id:
                    read_res = await run_herdr_cmd(["pane", "read", pane_id, "--lines", "15"])
                    if read_res["success"]:
                        cleaned = strip_ansi(read_res["stdout"]).strip()
                        if cleaned:
                            terminal_snippet = cleaned[-1500:]

                msg_text = build_idle_message_text(
                    agent_name=agent_name,
                    pane_id=pane_id,
                    workspace_id=workspace_id,
                    terminal_snippet=terminal_snippet,
                    cwd=cwd,
                    title=terminal_title
                )
                reply_markup = build_idle_keyboard(pane_id, agent_name)

                for chat_id in NOTIFICATION_CHAT_IDS:
                    try:
                        sent_msg = await context.bot.send_message(
                            chat_id=chat_id,
                            text=msg_text,
                            parse_mode="Markdown",
                            reply_markup=reply_markup
                        )
                        notification_message_meta[sent_msg.message_id] = {
                            "pane_id": pane_id,
                            "agent_name": agent_name,
                            "workspace_id": workspace_id,
                            "status_raw": status_raw,
                            "terminal_snippet": terminal_snippet,
                            "type": "idle_notification",
                        }
                    except Exception as e:
                        logger.warning(f"Lỗi gửi tin nhắn Markdown tới {chat_id}: {e}. Thử gửi dạng text thuần...")
                        try:
                            sent_msg = await context.bot.send_message(
                                chat_id=chat_id,
                                text=strip_ansi(msg_text).replace("*", "").replace("`", ""),
                                reply_markup=reply_markup
                            )
                            notification_message_meta[sent_msg.message_id] = {
                                "pane_id": pane_id,
                                "agent_name": agent_name,
                                "workspace_id": workspace_id,
                                "status_raw": status_raw,
                                "terminal_snippet": terminal_snippet,
                                "type": "idle_notification",
                            }
                        except Exception as e2:
                            logger.error(f"Lỗi gửi thông báo hoàn thành công việc tới chat {chat_id}: {e2}")

                prev["last_idle_notified_seq"] = seq

            # Cập nhật trạng thái sau khi xử lý idle
            prev["was_working"] = False
            prev["status"] = status
            prev["status_raw"] = status_raw
            prev["seq"] = seq
            prev["completion_seq"] = completion_seq
            prev["cwd"] = cwd
            prev["terminal_title"] = terminal_title

    # Dọn dẹp các agent đã kết thúc / đóng pane
    for dead_key in list(monitored_agent_states.keys()):
        if dead_key not in active_keys:
            del monitored_agent_states[dead_key]

# ------------------------------------------------------------------------------
# Bot Command Handlers
# ------------------------------------------------------------------------------
@check_permission
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /start hoặc /help."""
    text = (
        "🤖 *Herdr Remote Control Bridge*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Hệ thống điều khiển & giám sát AI Coding Agents từ xa.\n\n"
        "📋 *Danh mục lệnh hỗ trợ:*\n"
        "• `/status` — Kiểm tra trạng thái hệ thống Herdr Server & Client\n"
        "• `/workspaces` — Danh sách các workspace đang hoạt động\n"
        "• `/agents` — Danh sách các AI Agents, trạng thái & nút điều khiển\n"
        "• `/logs <pane_id>` — Xem 30 dòng log mới nhất của pane\n"
        "• `/prompt <agent> <msg>` — Gửi chỉ thị mới cho agent\n"
        "• `/reply [pane_id] <msg>` — Gửi phản hồi / văn bản trực tiếp vào terminal pane\n"
        "• `/key [pane_id] <phím>` — Gửi phím điều hướng (enter, esc, up, down, space, tab)\n"
        "• `/stop <pane_id>` — Dừng tiến trình bằng tín hiệu Ctrl+C\n\n"
        "💡 *Tương tác khi Agent chờ duyệt / đưa ra lựa chọn:*\n"
        "1. **Nút lựa chọn tự động:** Bot tự nhận diện các menu số `1️⃣, 2️⃣, 3️⃣...` hoặc phím tắt `[Y], [N]` để bạn bấm chọn 1 chạm.\n"
        "2. **🎮 Bàn phím TUI:** Điều khiển menu mũi tên (⬆️ Lên, ⬇️ Xuống, ␣ Space, ⏎ Enter) với màn hình cập nhật trực tiếp.\n"
        "3. **🔢 Phím số nhanh:** Hàng nút 1️⃣ đến 5️⃣ cho các câu hỏi chọn số.\n"
        "4. **Nhập tự do:** Bạn chỉ cần vuốt **Reply** trực tiếp tin nhắn thông báo hoặc gửi tin nhắn text khi có agent đang chờ.\n\n"
        "🎉 *Tự động phát hiện hoàn thành công việc (Working ➔ Idle):*\n"
        "• Khi agent hoàn thành nhiệm vụ và chuyển sang rảnh rỗi, bot tự động gửi thông báo kèm output cuối cùng và nút giao việc tiếp theo nhanh chóng."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

@check_permission
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /status: kiểm tra runtime của Herdr Server & Client."""
    res = await run_herdr_cmd(["status", "--json"])
    if res["success"]:
        try:
            data = json.loads(res["stdout"])
            text = format_status_card(data, res["stdout"])
        except Exception:
            text = f"🖥️ *Herdr Status:*\n```text\n{res['stdout']}\n```"
    else:
        fallback_res = await run_herdr_cmd(["status", "server"])
        err_msg = fallback_res["stderr"] or fallback_res["stdout"] or res["stderr"]
        text = f"🔴 *Herdr Server Status: OFFLINE / ERROR*\n```text\n{err_msg}\n```"
    await update.message.reply_text(text, parse_mode="Markdown")

@check_permission
async def cmd_workspaces(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /workspaces: Liệt kê các workspaces đang chạy dạng thẻ trực quan."""
    res = await run_herdr_cmd(["workspace", "list"])
    if not res["success"] or not res["stdout"]:
        err_text = res["stderr"] if res["stderr"] else "Không thể kết nối tới Herdr Server."
        await update.message.reply_text(f"⚠️ *Lỗi lấy danh sách Workspaces:*\n```text\n{err_text}\n```", parse_mode="Markdown")
        return

    text = format_workspaces_card(res["stdout"])
    await update.message.reply_text(text, parse_mode="Markdown")

@check_permission
async def cmd_agents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /agents: Liệt kê các AI Agents dạng thẻ và nút hành động."""
    res = await run_herdr_cmd(["agent", "list"])
    if not res["success"]:
        err_text = res["stderr"] if res["stderr"] else "Không thể kết nối tới Herdr Server."
        await update.message.reply_text(f"⚠️ *Lỗi lấy danh sách Agents:*\n```text\n{err_text}\n```", parse_mode="Markdown")
        return

    text, reply_markup = format_agents_card(res["stdout"])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)

@check_permission
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /logs <pane_id>: Đọc output gần nhất từ pane sạch sẽ không chứa ANSI."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Vui lòng cung cấp Pane ID:\n"
            "Sử dụng: `/logs <pane_id>`\n"
            "Ví dụ: `/logs w3:p1`",
            parse_mode="Markdown"
        )
        return

    pane_id = context.args[0].strip()
    res = await run_herdr_cmd(["pane", "read", pane_id, "--lines", "30"])
    if not res["success"]:
        await update.message.reply_text(f"❌ Không thể đọc log từ pane `{pane_id}`:\n```text\n{res['stderr']}\n```", parse_mode="Markdown")
        return

    raw_text = strip_ansi(res["stdout"]).strip()
    if not raw_text:
        raw_text = "(Pane hiện chưa có nội dung hiển thị)"
    elif len(raw_text) > 3500:
        raw_text = "..." + raw_text[-3500:]

    text = (
        f"📜 *LOGS PANE `{pane_id}`* _(30 dòng gần nhất)_\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"```text\n{raw_text}\n```"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

@check_permission
async def cmd_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /prompt <agent_name> <nội dung prompt>: Gửi chỉ thị đến agent."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Cú pháp: `/prompt <tên_agent> <nội dung prompt>`\n"
            "Ví dụ: `/prompt agy Kiểm tra lại mã nguồn`",
            parse_mode="Markdown"
        )
        return

    agent_name = context.args[0].strip()
    prompt_text = " ".join(context.args[1:]).strip()

    await update.message.reply_text(f"⏳ Đang chuyển prompt tới `{agent_name}`...", parse_mode="Markdown")
    res = await run_herdr_cmd(["agent", "prompt", agent_name, prompt_text])
    if res["success"]:
        await update.message.reply_text(f"✅ Đã gửi prompt thành công tới `{agent_name}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Lỗi gửi prompt tới `{agent_name}`:\n{res['stderr']}", parse_mode="Markdown")

@check_permission
async def cmd_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /reply [pane_id] <nội dung>: Gửi chuỗi văn bản kèm phím Enter trực tiếp vào terminal pane."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Cú pháp: `/reply [pane_id] <nội dung>`\n"
            "Ví dụ có pane_id: `/reply w3:p1 1`\n"
            "Ví dụ rút gọn khi có agent chờ: `/reply 1` hoặc `/reply y`",
            parse_mode="Markdown"
        )
        return

    first_arg = context.args[0].strip()
    if ":" in first_arg and len(context.args) >= 2:
        pane_id = first_arg
        reply_text = " ".join(context.args[1:]).strip()
    else:
        blocked_panes = [info["pane_id"] for info in monitored_agent_states.values() if info.get("is_blocked") and info.get("pane_id")]
        if len(blocked_panes) == 1:
            pane_id = blocked_panes[0]
            reply_text = " ".join(context.args).strip()
        elif len(blocked_panes) > 1:
            await update.message.reply_text(
                "⚠️ Đang có nhiều hơn 1 agent chờ duyệt. Vui lòng chỉ định rõ Pane ID:\n"
                f"Các pane đang chờ: {', '.join([f'`{p}`' for p in blocked_panes])}\n"
                "Cú pháp: `/reply <pane_id> <nội dung>`",
                parse_mode="Markdown"
            )
            return
        else:
            await update.message.reply_text(
                "⚠️ Vui lòng cung cấp Pane ID:\n"
                "Cú pháp: `/reply <pane_id> <nội dung>`\n"
                "Ví dụ: `/reply w3:p1 y`",
                parse_mode="Markdown"
            )
            return

    pane_id = await resolve_valid_pane_id(pane_id)
    await run_herdr_cmd(["pane", "send-text", pane_id, reply_text])
    res = await run_herdr_cmd(["pane", "send-keys", pane_id, "enter"])
    if res["success"]:
        await update.message.reply_text(f"✅ Đã gửi phản hồi vào Pane `{pane_id}`:\n```text\n{reply_text}\n```", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Lỗi gửi phản hồi vào Pane `{pane_id}`:\n{res['stderr']}", parse_mode="Markdown")

@check_permission
async def cmd_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /key [pane_id] <phím>: Gửi phím điều hướng/điều khiển (enter, esc, up, down, space, tab, ctrl+c)."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Cú pháp: `/key [pane_id] <tên_phím>`\n"
            "Các phím hỗ trợ: `enter`, `esc`, `up`, `down`, `space`, `tab`, `ctrl+c`\n"
            "Ví dụ: `/key enter` (tự chọn agent đang chờ)\n"
            "Ví dụ: `/key w3:p1 up`",
            parse_mode="Markdown"
        )
        return

    first_arg = context.args[0].strip().lower()
    if ":" in first_arg and len(context.args) >= 2:
        pane_id = first_arg
        key_name = context.args[1].strip().lower()
    else:
        blocked_panes = [info["pane_id"] for info in monitored_agent_states.values() if info.get("is_blocked") and info.get("pane_id")]
        if len(blocked_panes) == 1:
            pane_id = blocked_panes[0]
            key_name = first_arg
        elif len(blocked_panes) > 1:
            await update.message.reply_text(
                "⚠️ Đang có nhiều hơn 1 agent chờ duyệt. Vui lòng chỉ định rõ Pane ID:\n"
                "Cú pháp: `/key <pane_id> <phím>`",
                parse_mode="Markdown"
            )
            return
        else:
            await update.message.reply_text(
                "⚠️ Không tìm thấy agent đang chờ duyệt. Vui lòng chỉ định rõ Pane ID:\n"
                "Cú pháp: `/key <pane_id> <phím>`",
                parse_mode="Markdown"
            )
            return

    pane_id = await resolve_valid_pane_id(pane_id)
    res = await run_herdr_cmd(["pane", "send-keys", pane_id, key_name])
    if res["success"]:
        await update.message.reply_text(f"✅ Đã gửi phím `{key_name}` tới Pane `{pane_id}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Lỗi gửi phím `{key_name}` tới Pane `{pane_id}`:\n{res['stderr']}", parse_mode="Markdown")

@check_permission
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /stop <pane_id hoặc agent_name>: Gửi Ctrl+C dừng tiến trình."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Vui lòng cung cấp Pane ID hoặc tên Agent cần dừng:\n"
            "Sử dụng: `/stop <pane_id>` (ví dụ: `/stop w3:p1`)\n"
            "Hoặc: `/stop <agent_name>`",
            parse_mode="Markdown"
        )
        return

    target = context.args[0].strip()
    if ":" in target:
        res = await run_herdr_cmd(["pane", "send-keys", target, "ctrl+c"])
    else:
        res = await run_herdr_cmd(["agent", "send-keys", target, "ctrl+c"])

    if res["success"]:
        await update.message.reply_text(f"🛑 Đã gửi tín hiệu dừng (`Ctrl+C`) tới `{target}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Không thể gửi tín hiệu dừng tới `{target}`:\n```text\n{res['stderr']}\n```", parse_mode="Markdown")

@check_permission
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý nút bấm tương tác inline (Duyệt/Từ chối, Chọn Option, Bàn phím TUI, Phím số, Log)."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user = update.effective_user
    username = f"@{user.username}" if user and user.username else f"ID {user.id}"

    prefix, pane_id, extra = parse_callback_data(data)
    pane_id = await resolve_valid_pane_id(pane_id)

    # 1. Phê duyệt mặc định (y)
    if prefix in ("autoapprove", "approve"):
        agent_name = extra
        success, detail = await approve_agent(pane_id, agent_name)
        if success:
            await query.edit_message_text(
                f"✅ *ĐÃ PHÊ DUYỆT BỞI {username}*\n"
                f"🤖 Agent: `{agent_name}`" + (f" | Pane: `{pane_id}`" if pane_id else "") + "\n"
                f"⚡ {detail}. Agent đang tiếp tục tiến trình.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(f"❌ Phê duyệt thất bại: {detail}", parse_mode="Markdown")

    # 2. Từ chối mặc định (n)
    elif prefix in ("autoreject", "reject"):
        agent_name = extra
        success, detail = await reject_agent(pane_id, agent_name)
        if success:
            await query.edit_message_text(
                f"❌ *ĐÃ TỪ CHỐI BỞI {username}*\n"
                f"🤖 Agent: `{agent_name}`" + (f" | Pane: `{pane_id}`" if pane_id else "") + "\n"
                f"🛑 {detail}.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(f"❌ Thao tác từ chối thất bại: {detail}", parse_mode="Markdown")

    # 3. Chọn lựa chọn cụ thể (số hoặc phím tắt)
    elif prefix == "c":
        choice_val = extra
        logger.info(f"Người dùng {username} bấm chọn lựa chọn [{choice_val}] cho Pane {pane_id}")
        await run_herdr_cmd(["pane", "send-text", pane_id, choice_val])
        r_enter = await run_herdr_cmd(["pane", "send-keys", pane_id, "enter"])
        if r_enter["success"]:
            await query.edit_message_text(
                f"✅ *ĐÃ CHỌN LỰA CHỌN [{choice_val}] BỞI {username}*\n"
                f"📍 Pane: `{pane_id}`\n"
                f"⚡ Đã gửi `{choice_val}` + Enter. Agent đang tiếp tục xử lý.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(f"❌ Lỗi khi gửi lựa chọn: {r_enter['stderr']}", parse_mode="Markdown")

    # 4. Phím điều khiển TUI (enter, esc, refresh, up, down, space, tab)
    elif prefix == "k":
        key = extra
        logger.info(f"Người dùng {username} bấm phím điều khiển [{key}] cho Pane {pane_id}")

        if key == "enter":
            res = await run_herdr_cmd(["pane", "send-keys", pane_id, "enter"])
            if res["success"]:
                await query.edit_message_text(
                    f"✅ *ĐÃ XÁC NHẬN (Enter) BỞI {username}*\n"
                    f"📍 Pane: `{pane_id}`\n"
                    f"⚡ Tiến trình đang tiếp tục.",
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text(f"❌ Lỗi gửi phím Enter: {res['stderr']}", parse_mode="Markdown")

        elif key == "esc":
            res = await run_herdr_cmd(["pane", "send-keys", pane_id, "esc"])
            if res["success"]:
                await query.edit_message_text(
                    f"⎋ *ĐÃ HỦY / THOÁT (Esc) BỞI {username}*\n"
                    f"📍 Pane: `{pane_id}`",
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text(f"❌ Lỗi gửi phím Esc: {res['stderr']}", parse_mode="Markdown")

        elif key == "refresh":
            read_res = await run_herdr_cmd(["pane", "read", pane_id, "--lines", "15"])
            cleaned = strip_ansi(read_res["stdout"]).strip()[-1500:] if read_res["success"] else "(Không có dữ liệu)"
            meta = notification_message_meta.get(query.message.message_id, {})
            agent_name = meta.get("agent_name", "Agent")
            ws_id = meta.get("workspace_id", "")
            status_raw = meta.get("status_raw", "blocked")
            msg_text = build_blocked_message_text(agent_name, pane_id, ws_id, status_raw, cleaned, mode="tui")
            reply_markup = build_approval_keyboard(pane_id, agent_name, cleaned, mode="tui")
            try:
                await query.edit_message_text(text=msg_text, reply_markup=reply_markup, parse_mode="Markdown")
            except Exception:
                pass

        elif key in ("up", "down", "space", "tab"):
            await run_herdr_cmd(["pane", "send-keys", pane_id, key])
            await asyncio.sleep(0.25)
            # Đọc lại output để hiển thị con trỏ mới
            read_res = await run_herdr_cmd(["pane", "read", pane_id, "--lines", "15"])
            cleaned = strip_ansi(read_res["stdout"]).strip()[-1500:] if read_res["success"] else "(Không có dữ liệu)"
            meta = notification_message_meta.get(query.message.message_id, {})
            agent_name = meta.get("agent_name", "Agent")
            ws_id = meta.get("workspace_id", "")
            status_raw = meta.get("status_raw", "blocked")
            msg_text = build_blocked_message_text(agent_name, pane_id, ws_id, status_raw, cleaned, mode="tui")
            reply_markup = build_approval_keyboard(pane_id, agent_name, cleaned, mode="tui")
            try:
                await query.edit_message_text(text=msg_text, reply_markup=reply_markup, parse_mode="Markdown")
            except Exception:
                pass

    # 5. Chuyển đổi giao diện bàn phím (main, tui, num)
    elif prefix == "mode":
        target_mode = extra
        read_res = await run_herdr_cmd(["pane", "read", pane_id, "--lines", "15"])
        cleaned = strip_ansi(read_res["stdout"]).strip()[-1500:] if read_res["success"] else "(Không có dữ liệu)"
        meta = notification_message_meta.get(query.message.message_id, {})
        agent_name = meta.get("agent_name", "Agent")
        ws_id = meta.get("workspace_id", "")
        status_raw = meta.get("status_raw", "blocked")
        msg_text = build_blocked_message_text(agent_name, pane_id, ws_id, status_raw, cleaned, mode=target_mode)
        reply_markup = build_approval_keyboard(pane_id, agent_name, cleaned, mode=target_mode)
        try:
            await query.edit_message_text(text=msg_text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception:
            pass

    # 6. Dừng tiến trình
    elif prefix == "autostop":
        agent_name = extra
        res = await run_herdr_cmd(["pane", "send-keys", pane_id, "ctrl+c"])
        if res["success"]:
            await query.edit_message_text(
                f"🛑 *ĐÃ DỪNG TIẾN TRÌNH (Ctrl+C) BỞI {username}*\n"
                f"🤖 Agent: `{agent_name}` | Pane: `{pane_id}`",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(f"❌ Không thể dừng tiến trình: {res['stderr']}", parse_mode="Markdown")

    # 7. Xem logs
    elif prefix in ("logs", "log"):
        res = await run_herdr_cmd(["pane", "read", pane_id, "--lines", "30"])
        if not res["success"]:
            await query.message.reply_text(f"❌ Không thể đọc log từ pane `{pane_id}`:\n```text\n{res['stderr']}\n```", parse_mode="Markdown")
            return

        raw_text = strip_ansi(res["stdout"]).strip()
        if not raw_text:
            raw_text = "(Pane hiện chưa có nội dung hiển thị)"
        elif len(raw_text) > 3500:
            raw_text = "..." + raw_text[-3500:]

        log_msg = (
            f"📜 *LOGS PANE `{pane_id}`* _(30 dòng gần nhất)_\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"```text\n{raw_text}\n```"
        )
        await query.message.reply_text(log_msg, parse_mode="Markdown")

    # 8. Hướng dẫn giao nhiệm vụ mới khi agent idle
    elif prefix in ("prompt_hint", "assign"):
        agent_name = extra or "agent"
        hint_text = (
            f"💬 *HƯỚNG DẪN GIAO VIỆC CHO AGENT `{agent_name}`*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"1️⃣ **Nhanh nhất (1 chạm):**\n"
            f"   Vuốt **Reply** trực tiếp tin nhắn thông báo hoàn thành của `{agent_name}` ở trên với nội dung nhiệm vụ mới.\n\n"
            f"2️⃣ **Dùng lệnh `/prompt`:**\n"
            f"   `/prompt {agent_name} <nội dung nhiệm vụ mới>`\n\n"
            f"3️⃣ **Gửi trực tiếp vào Terminal:**\n"
            f"   `/reply {pane_id} <nội dung>`"
        )
        await query.message.reply_text(hint_text, parse_mode="Markdown")

    # 9. Làm mới danh sách agents
    elif prefix == "refresh_agents":
        res = await run_herdr_cmd(["agent", "list"])
        if res["success"]:
            text, reply_markup = format_agents_card(res["stdout"])
            await query.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await query.message.reply_text(f"❌ Không thể lấy danh sách Agents: {res['stderr']}", parse_mode="Markdown")

@check_permission
async def handle_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Khi người dùng Reply trực tiếp một tin nhắn thông báo hoặc gửi tin nhắn text khi có agent chờ duyệt / hoàn thành."""
    if not update.message or not update.message.text:
        return

    user_input = update.message.text.strip()
    if not user_input:
        return

    pane_id = None
    agent_name = None
    msg_type = None

    if update.message.reply_to_message:
        replied_msg = update.message.reply_to_message
        # 1. Tìm trong mapping metadata tin nhắn
        meta = notification_message_meta.get(replied_msg.message_id)
        if meta:
            pane_id = meta.get("pane_id")
            agent_name = meta.get("agent_name")
            msg_type = meta.get("type")

        # 2. Nếu không thấy trong meta, regex tìm trong replied text (ưu tiên wX:pY)
        if not pane_id and replied_msg.text:
            match = re.search(r"Pane\s+`?([a-zA-Z0-9_\-]+:[a-zA-Z0-9_\-]+|[a-zA-Z0-9_\-]+)`?", replied_msg.text)
            if match:
                pane_id = match.group(1)
        if not agent_name and replied_msg.text:
            match_agent = re.search(r"Agent:\*?\s+`?([a-zA-Z0-9_\-]+)`?", replied_msg.text)
            if match_agent:
                agent_name = match_agent.group(1)

    # 3. Nếu vẫn không có pane_id, kiểm tra xem có đúng 1 agent đang blocked không
    if not pane_id:
        blocked_panes = [info["pane_id"] for info in monitored_agent_states.values() if info.get("is_blocked") and info.get("pane_id")]
        if len(blocked_panes) == 1:
            pane_id = blocked_panes[0]

    # 4. Nếu không có agent blocked, kiểm tra xem có đúng 1 agent đang idle không
    if not pane_id:
        idle_agents = [
            (info["pane_id"], info.get("agent_name", ""))
            for info in monitored_agent_states.values()
            if not info.get("is_blocked") and any(k in info.get("status", "") for k in ("idle", "ready", "done")) and info.get("pane_id")
        ]
        if len(idle_agents) == 1:
            pane_id, agent_name = idle_agents[0]

    pane_id = await resolve_valid_pane_id(pane_id)

    if not pane_id:
        # Nếu người dùng nhắn tin tự do mà không reply và không có agent nào blocked / idle duy nhất
        if not update.message.reply_to_message:
            await update.message.reply_text(
                "ℹ️ Tin nhắn nhận được. Hiện không có Agent nào đang chờ duyệt hoặc rảnh rỗi duy nhất để tự động chuyển tiếp.\n"
                "Dùng `/agents` để xem danh sách hoặc `/help` để xem các lệnh hỗ trợ.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "⚠️ Không xác định được Pane ID từ tin nhắn này. Vui lòng dùng lệnh `/reply <pane_id> <nội dung>` hoặc `/prompt <agent> <nội dung>`.",
                parse_mode="Markdown"
            )
        return

    logger.info(f"Chuyển nội dung người dùng nhập tới Pane {pane_id} (Agent: {agent_name or 'N/A'}): {user_input}")
    await run_herdr_cmd(["pane", "send-text", pane_id, user_input])
    r_enter = await run_herdr_cmd(["pane", "send-keys", pane_id, "enter"])

    if r_enter["success"]:
        action_desc = "giao nhiệm vụ mới cho" if (msg_type == "idle_notification" or not any(info.get("is_blocked") for info in monitored_agent_states.values() if info.get("pane_id") == pane_id)) else "gửi phản hồi tới"
        agent_str = f" Agent `{agent_name}` (" if agent_name else " "
        agent_end = ")" if agent_name else ""
        await update.message.reply_text(
            f"✅ Đã {action_desc}{agent_str}Pane `{pane_id}`{agent_end}:\n```text\n{user_input}\n```",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ Không thể gửi tới Pane `{pane_id}`:\n```text\n{r_enter['stderr']}\n```",
            parse_mode="Markdown"
        )

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Xử lý lỗi toàn cục và tự động bỏ qua/thử lại khi gặp lỗi mạng tạm thời (Timeout, NetworkError)."""
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning(f"Sự cố mạng tạm thời tới Telegram API ({type(err).__name__}: {err}). Bot sẽ tự động thử lại...")
        return
    if isinstance(err, RetryAfter):
        logger.warning(f"Telegram yêu cầu đợi {err.retry_after}s trước khi gửi tiếp.")
        return
    logger.error(f"Lỗi không mong muốn trong quá trình xử lý update: {err}", exc_info=context.error)

# ------------------------------------------------------------------------------
# Khởi động ứng dụng
# ------------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        logger.critical("Lỗi: Biến môi trường TELEGRAM_BOT_TOKEN chưa được cấu hình.")
        sys.exit(1)

    if not ALLOWED_USERS:
        logger.critical("Lỗi: Biến môi trường ALLOWED_TELEGRAM_USER_IDS chưa được cấu hình. Không thể chạy ở chế độ mở hoàn toàn vì lý do an ninh.")
        sys.exit(1)

    logger.info(f"Khởi động Herdr Telegram Bridge với {len(ALLOWED_USERS)} users được cấp phép.")
    if TELEGRAM_PROXY:
        logger.info(f"Sử dụng Proxy cho Telegram API: {TELEGRAM_PROXY}")
    logger.info(f"Cấu hình Telegram Timeout: connect={TELEGRAM_CONNECT_TIMEOUT}s, read={TELEGRAM_READ_TIMEOUT}s, write={TELEGRAM_WRITE_TIMEOUT}s.")

    if AUTO_NOTIFY_BLOCKED or AUTO_NOTIFY_IDLE:
        logger.info(f"Bật chế độ tự động giám sát (Blocked: {AUTO_NOTIFY_BLOCKED}, Idle/Hoàn thành: {AUTO_NOTIFY_IDLE}, chu kỳ: {POLL_INTERVAL_SEC}s).")

    # Khởi tạo HTTP client với timeout cao chống lỗi mạng chập chờn
    httpx_request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=TELEGRAM_READ_TIMEOUT,
        write_timeout=TELEGRAM_WRITE_TIMEOUT,
        pool_timeout=TELEGRAM_POOL_TIMEOUT,
        proxy=TELEGRAM_PROXY,
    )
    get_updates_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=TELEGRAM_READ_TIMEOUT + 15.0,
        write_timeout=TELEGRAM_WRITE_TIMEOUT,
        pool_timeout=TELEGRAM_POOL_TIMEOUT,
        proxy=TELEGRAM_PROXY,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(httpx_request)
        .get_updates_request(get_updates_request)
        .build()
    )

    # Đăng ký handler xử lý lỗi toàn cục
    app.add_error_handler(global_error_handler)

    # Đăng ký job nền định kỳ để giám sát agent (Blocked & Hoàn thành/Idle)
    if (AUTO_NOTIFY_BLOCKED or AUTO_NOTIFY_IDLE) and app.job_queue:
        app.job_queue.run_repeating(monitor_agents_job, interval=POLL_INTERVAL_SEC, first=2)

    # Đăng ký lệnh bot
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("workspaces", cmd_workspaces))
    app.add_handler(CommandHandler("agents", cmd_agents))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("prompt", cmd_prompt))
    app.add_handler(CommandHandler("reply", cmd_reply))
    app.add_handler(CommandHandler("key", cmd_key))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Xử lý khi người dùng Reply tin nhắn thông báo hoặc gửi tin nhắn văn bản
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_user_reply))

    # Chạy bot ở chế độ long-polling với cơ chế tự động thử lại khi gặp sự cố mạng lúc khởi động
    app.run_polling(
        drop_pending_updates=True,
        bootstrap_retries=10,  # Tự động thử lại 10 lần nếu lúc khởi động mạng bị timeout
        timeout=15,            # Thời gian timeout cho mỗi vòng getUpdates
    )

if __name__ == "__main__":
    main()
