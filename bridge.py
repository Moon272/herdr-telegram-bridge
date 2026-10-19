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
"""

import os
import sys
import json
import time
import re
import logging
import asyncio
import subprocess
from typing import List, Optional, Dict, Any, Tuple
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
ALLOWED_USERS = set()
if raw_allowlist:
    for uid in raw_allowlist.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ALLOWED_USERS.add(int(uid))

RATE_LIMIT_MAX_CALLS = int(os.environ.get("RATE_LIMIT_MAX_CALLS", "10"))
RATE_LIMIT_PERIOD_SEC = int(os.environ.get("RATE_LIMIT_PERIOD_SEC", "60"))

# Rate limiting storage: user_id -> [timestamps]
user_call_history: Dict[int, List[float]] = defaultdict(list)

# ------------------------------------------------------------------------------
# Helper & Security Decorators
# ------------------------------------------------------------------------------
def is_rate_limited(user_id: int) -> bool:
    """Kiểm tra giới hạn tần suất gọi lệnh."""
    now = time.time()
    history = user_call_history[user_id]
    # Lọc các lần gọi trong khung thời gian
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

# ------------------------------------------------------------------------------
# Visual Formatting & UI Helpers (Option 1: Semantic Rich Cards)
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
            name = ag.get("name") or ag.get("agent_id") or ag.get("id") or "Unknown"
            kind = ag.get("kind") or ag.get("type") or "Agent"
            pane_id = ag.get("pane_id", "")
            status_raw = ag.get("status") or ag.get("lifecycle_state") or ag.get("state") or "unknown"
            status_badge = format_agent_status_badge(status_raw)
            ws_id = ag.get("workspace_id", "")

            loc_str = f"`{pane_id}`" if pane_id else f"`{ws_id}`"
            cards.append(
                f"👤 *Agent:* `{name}` `[{kind}]`\n"
                f"   ▫️ *Vị trí:* Pane {loc_str}\n"
                f"   ▫️ *Trạng thái:* {status_badge}"
            )

            # Nút tương tác cho từng agent
            row_buttons = []
            if "blocked" in status_raw.lower() or "wait" in status_raw.lower():
                row_buttons.extend([
                    InlineKeyboardButton(f"✅ Duyệt {name}", callback_data=f"approve_{name}"),
                    InlineKeyboardButton(f"❌ Từ chối {name}", callback_data=f"reject_{name}")
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
        "• `/logs <pane_id>` — Xem 25 dòng log mới nhất của pane\n"
        "• `/prompt <agent> <msg>` — Gửi chỉ thị mới cho agent\n"
        "• `/stop <pane_id>` — Dừng tiến trình bằng tín hiệu Ctrl+C\n"
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
            "Ví dụ: `/logs w1:p2`",
            parse_mode="Markdown"
        )
        return

    pane_id = context.args[0].strip()
    res = await run_herdr_cmd(["pane", "read", pane_id, "--lines", "25"])
    if not res["success"]:
        await update.message.reply_text(f"❌ Không thể đọc log từ pane `{pane_id}`:\n```text\n{res['stderr']}\n```", parse_mode="Markdown")
        return

    raw_text = strip_ansi(res["stdout"]).strip()
    if not raw_text:
        raw_text = "(Pane hiện chưa có nội dung hiển thị)"
    elif len(raw_text) > 3500:
        raw_text = "..." + raw_text[-3500:]

    text = (
        f"📜 *LOGS PANE `{pane_id}`* _(25 dòng gần nhất)_\n"
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
            "Ví dụ: `/prompt reviewer Kiểm tra lại hàm authenticate`",
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
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /stop <pane_id hoặc agent_name>: Gửi Ctrl+C dừng tiến trình."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Vui lòng cung cấp Pane ID hoặc tên Agent cần dừng:\n"
            "Sử dụng: `/stop <pane_id>` (ví dụ: `/stop w1:p2`)\n"
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
    if data.startswith("approve_"):
        agent_id = data.replace("approve_", "")
        res = await run_herdr_cmd(["agent", "prompt", agent_id, "y"])
        if res["success"]:
            await query.edit_message_text(f"✅ Đã phê chuẩn yêu cầu tiếp tục của agent `{agent_id}`.", parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ Thao tác duyệt thất bại: {res['stderr']}", parse_mode="Markdown")

    elif data.startswith("reject_"):
        agent_id = data.replace("reject_", "")
        res = await run_herdr_cmd(["agent", "prompt", agent_id, "n"])
        if res["success"]:
            await query.edit_message_text(f"❌ Đã từ chối hành động của agent `{agent_id}`.", parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ Thao tác từ chối thất bại: {res['stderr']}", parse_mode="Markdown")

    elif data.startswith("logs_"):
        pane_id = data.replace("logs_", "")
        res = await run_herdr_cmd(["pane", "read", pane_id, "--lines", "25"])
        if not res["success"]:
            await query.message.reply_text(f"❌ Không thể đọc log từ pane `{pane_id}`:\n```text\n{res['stderr']}\n```", parse_mode="Markdown")
            return

        raw_text = strip_ansi(res["stdout"]).strip()
        if not raw_text:
            raw_text = "(Pane hiện chưa có nội dung hiển thị)"
        elif len(raw_text) > 3500:
            raw_text = "..." + raw_text[-3500:]

        log_msg = (
            f"📜 *LOGS PANE `{pane_id}`* _(25 dòng gần nhất)_\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"```text\n{raw_text}\n```"
        )
        await query.message.reply_text(log_msg, parse_mode="Markdown")

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

    app = Application.builder().token(BOT_TOKEN).build()

    # Đăng ký lệnh
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("workspaces", cmd_workspaces))
    app.add_handler(CommandHandler("agents", cmd_agents))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("prompt", cmd_prompt))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Chạy bot ở chế độ long-polling
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

