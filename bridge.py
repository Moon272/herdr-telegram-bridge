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

# ------------------------------------------------------------------------------
# Visual Formatting & UI Helpers
# ------------------------------------------------------------------------------
ANSI_REGEX = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def strip_ansi(text: str) -> str:
    """Loại bỏ các mã màu ANSI terminal để text log sạch sẽ."""
    return ANSI_REGEX.sub('', text) if text else ""

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
                    InlineKeyboardButton(f"✅ Duyệt {name}", callback_data=f"autoapprove:{pane_id}:{name}"),
                    InlineKeyboardButton(f"❌ Từ chối {name}", callback_data=f"autoreject:{pane_id}:{name}")
                ])
            if pane_id:
                row_buttons.append(
                    InlineKeyboardButton(f"📜 Log {name}", callback_data=f"logs_{pane_id}")
                )
            if row_buttons:
                keyboard.append(row_buttons)

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        return "\n\n".join(cards), reply_markup
    except Exception as e:
        logger.error(f"Lỗi format agents card: {e}")
        return f"🤖 *Danh sách AI Agents:*\n```text\n{raw_stdout}\n```", None

# ------------------------------------------------------------------------------
# Proactive Background Monitor (Tự động gửi thông báo phê duyệt)
# ------------------------------------------------------------------------------
async def monitor_agents_job(context: ContextTypes.DEFAULT_TYPE):
    """Job nền chạy lặp lại định kỳ để phát hiện agent bị Blocked và tự động gửi thông báo duyệt."""
    if not AUTO_NOTIFY_BLOCKED or not NOTIFICATION_CHAT_IDS:
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
        key = f"{pane_id}:{agent_name}"
        active_keys.add(key)

        is_blocked = any(k in status for k in ("blocked", "wait", "question", "approval"))

        if is_blocked:
            prev = monitored_agent_states.get(key)
            # Nếu chưa từng thông báo hoặc số thứ tự thay đổi (lần hỏi mới) hoặc trước đó không blocked
            if not prev or prev.get("seq") != seq or not prev.get("is_blocked"):
                logger.info(f"Phát hiện Agent `{agent_name}` (Pane `{pane_id}`) chuyển sang trạng thái BLOCKED! Đang gửi thông báo duyệt...")

                # Trích xuất 15 dòng log gần nhất để người dùng nắm được ngữ cảnh agent đang xin duyệt gì
                terminal_snippet = "(Không có nội dung hiển thị trong terminal)"
                if pane_id:
                    read_res = await run_herdr_cmd(["pane", "read", pane_id, "--lines", "15"])
                    if read_res["success"]:
                        cleaned = strip_ansi(read_res["stdout"]).strip()
                        if cleaned:
                            terminal_snippet = cleaned[-1500:]

                msg_text = (
                    "🚨 *YÊU CẦU PHÊ DUYỆT TỪ AI AGENT!*\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🤖 *Agent:* `{agent_name}`\n"
                    f"📍 *Vị trí:* Pane `{pane_id}` (Workspace `{workspace_id}`)\n"
                    f"⚠️ *Trạng thái:* {format_agent_status_badge(status_raw)}\n\n"
                    f"📋 *Nội dung terminal gần nhất:*\n"
                    f"```text\n{terminal_snippet}\n```\n"
                    "👉 _Bấm nút bên dưới để Duyệt / Từ chối hoặc **Reply** trực tiếp tin nhắn này._"
                )

                keyboard = [
                    [
                        InlineKeyboardButton("✅ Phê duyệt (y)", callback_data=f"autoapprove:{pane_id}:{agent_name}"),
                        InlineKeyboardButton("❌ Từ chối (n)", callback_data=f"autoreject:{pane_id}:{agent_name}"),
                    ],
                    [
                        InlineKeyboardButton("📜 Xem 30 dòng Log", callback_data=f"logs_{pane_id}"),
                        InlineKeyboardButton("🛑 Dừng (Ctrl+C)", callback_data=f"autostop:{pane_id}:{agent_name}"),
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

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
                    except Exception as e:
                        logger.error(f"Lỗi gửi thông báo tự động tới chat {chat_id}: {e}")

                monitored_agent_states[key] = {
                    "seq": seq,
                    "is_blocked": True,
                    "status": status,
                    "pane_id": pane_id,
                    "agent_name": agent_name,
                    "messages": sent_records,
                    "notified_at": time.time(),
                }
        else:
            # Agent không còn blocked (ví dụ: đã chuyển sang working, idle, done)
            if key in monitored_agent_states and monitored_agent_states[key].get("is_blocked"):
                prev = monitored_agent_states[key]
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
                monitored_agent_states[key]["is_blocked"] = False
                monitored_agent_states[key]["status"] = status
                monitored_agent_states[key]["seq"] = seq

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
        "• `/agents` — Danh sách các AI Agents & nút phê duyệt\n"
        "• `/logs <pane_id>` — Xem 30 dòng log mới nhất của pane\n"
        "• `/prompt <agent> <msg>` — Gửi chỉ thị mới cho agent\n"
        "• `/reply <pane_id> <msg>` — Gửi phản hồi / văn bản trực tiếp vào terminal pane\n"
        "• `/stop <pane_id>` — Dừng tiến trình bằng tín hiệu Ctrl+C\n\n"
        "💡 *Tự động phê duyệt:* Khi agent yêu cầu xác nhận, bot sẽ chủ động gửi tin nhắn kèm nút bấm và bạn cũng có thể Reply tin nhắn đó."
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
    """Lệnh /reply <pane_id> <nội dung>: Gửi chuỗi văn bản kèm phím Enter trực tiếp vào terminal pane."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Cú pháp: `/reply <pane_id> <nội dung>`\n"
            "Ví dụ: `/reply w3:p1 y`\n"
            "Ví dụ: `/reply w3:p1 1`",
            parse_mode="Markdown"
        )
        return

    pane_id = context.args[0].strip()
    reply_text = " ".join(context.args[1:]).strip()

    await run_herdr_cmd(["pane", "send-text", pane_id, reply_text])
    res = await run_herdr_cmd(["pane", "send-keys", pane_id, "enter"])
    if res["success"]:
        await update.message.reply_text(f"✅ Đã gửi phản hồi vào Pane `{pane_id}`:\n```text\n{reply_text}\n```", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Lỗi gửi phản hồi vào Pane `{pane_id}`:\n{res['stderr']}", parse_mode="Markdown")

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
    """Xử lý nút bấm tương tác inline (Duyệt/Từ chối khi agent blocked, hoặc xem log)."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user = update.effective_user
    username = f"@{user.username}" if user and user.username else f"ID {user.id}"

    if data.startswith("autoapprove:") or data.startswith("approve_"):
        if ":" in data:
            _, pane_id, agent_name = data.split(":", 2)
        else:
            agent_name = data.replace("approve_", "")
            pane_id = ""

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

    elif data.startswith("autoreject:") or data.startswith("reject_"):
        if ":" in data:
            _, pane_id, agent_name = data.split(":", 2)
        else:
            agent_name = data.replace("reject_", "")
            pane_id = ""

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

    elif data.startswith("autostop:"):
        _, pane_id, agent_name = data.split(":", 2)
        res = await run_herdr_cmd(["pane", "send-keys", pane_id, "ctrl+c"])
        if res["success"]:
            await query.edit_message_text(
                f"🛑 *ĐÃ DỪNG TIẾN TRÌNH (Ctrl+C) BỞI {username}*\n"
                f"🤖 Agent: `{agent_name}` | Pane: `{pane_id}`",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(f"❌ Không thể dừng tiến trình: {res['stderr']}", parse_mode="Markdown")

    elif data.startswith("logs_"):
        pane_id = data.replace("logs_", "")
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

@check_permission
async def handle_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Khi người dùng Reply trực tiếp một tin nhắn thông báo trên Telegram để nhập văn bản vào terminal."""
    if not update.message or not update.message.reply_to_message:
        return

    replied_text = update.message.reply_to_message.text or ""
    user_input = update.message.text.strip()
    if not user_input:
        return

    # Tìm pane_id từ tin nhắn gốc (ví dụ: Pane `w3:p1`)
    match = re.search(r"Pane\s+`([a-zA-Z0-9_\-:]+)`", replied_text)
    if not match:
        return

    pane_id = match.group(1)
    logger.info(f"Người dùng reply trực tiếp vào Pane {pane_id}: {user_input}")

    await run_herdr_cmd(["pane", "send-text", pane_id, user_input])
    r_enter = await run_herdr_cmd(["pane", "send-keys", pane_id, "enter"])

    if r_enter["success"]:
        await update.message.reply_text(
            f"✅ Đã chuyển nội dung tới Pane `{pane_id}`:\n```text\n{user_input}\n```",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ Không thể gửi tới Pane `{pane_id}`:\n```text\n{r_enter['stderr']}\n```",
            parse_mode="Markdown"
        )

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
    if AUTO_NOTIFY_BLOCKED:
        logger.info(f"Bật chế độ tự động giám sát và thông báo duyệt (chu kỳ: {POLL_INTERVAL_SEC}s).")

    app = Application.builder().token(BOT_TOKEN).build()

    # Đăng ký job nền định kỳ để giám sát agent và gửi thông báo duyệt
    if AUTO_NOTIFY_BLOCKED and app.job_queue:
        app.job_queue.run_repeating(monitor_agents_job, interval=POLL_INTERVAL_SEC, first=2)

    # Đăng ký lệnh bot
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("workspaces", cmd_workspaces))
    app.add_handler(CommandHandler("agents", cmd_agents))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("prompt", cmd_prompt))
    app.add_handler(CommandHandler("reply", cmd_reply))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Xử lý khi người dùng Reply tin nhắn thông báo
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_user_reply))

    # Chạy bot ở chế độ long-polling
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
