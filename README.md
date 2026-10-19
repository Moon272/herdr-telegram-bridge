# Herdr Telegram Bridge 🚀

Cổng điều khiển và giám sát từ xa an toàn cho **Herdr** (AI Coding Agent Workspace Manager) qua **Telegram Bot**.

---

## 📌 Tính năng chính

- **Kiểm tra trạng thái hệ thống**: Giám sát Herdr Server & Client trực tiếp từ Telegram (`/status`).
- **Quản lý Workspaces & AI Agents**: Liệt kê các workspace đang hoạt động (`/workspaces`), danh sách các AI Agents (`/agents`).
- **Tương tác trực tiếp & Phê chuẩn (Interactive Approval)**: Hỗ trợ nút bấm duyệt (`approve`) hoặc từ chối (`reject`) khi agent rơi vào trạng thái blocked chờ lệnh.
- **Xem Log thời gian thực**: Trích xuất log gần nhất của từng pane (`/logs <pane_id>`).
- **Gửi chỉ thị (Prompt Injection)**: Gửi prompt/chỉ thị mới tới agent từ xa (`/prompt <agent_name> <nội dung>`).
- **Bảo mật đa tầng**:
  - Whitelist Telegram User ID nghiêm ngặt (chỉ cho phép user được khai báo).
  - Rate Limiting chống spam/DoS.
  - Audit logging lưu vết toàn bộ hoạt động vào file `logs/audit_bridge.log`.

---

## 🛠️ Yêu cầu môi trường

1. **Python 3.10+** (Hệ thống đã có Python và Herdr CLI).
2. **Herdr CLI** đã cài đặt và cấu hình sẵn (`herdr` có thể gọi được từ terminal).
3. **Telegram Bot Token** (lấy từ [@BotFather](https://t.me/BotFather)).
4. **Telegram User ID** của bạn (lấy từ [@userinfobot](https://t.me/userinfobot)).

---

## ⚙️ Cấu hình `.env`

Tạo hoặc chỉnh sửa file `.env` tại thư mục gốc:

```env
# Token từ @BotFather
TELEGRAM_BOT_TOKEN="your_bot_token_here"

# Danh sách ID Telegram được phép (ngăn cách bằng dấu phẩy)
ALLOWED_TELEGRAM_USER_IDS="1937362306"

# Cấu hình giới hạn tần suất lệnh (Rate Limiting)
RATE_LIMIT_MAX_CALLS=15
RATE_LIMIT_PERIOD_SEC=60
```

---

## 🚀 Hướng dẫn khởi chạy

### Cách 1: Chạy trực tiếp trên Windows (Khuyến nghị)

1. **Tạo và kích hoạt môi trường ảo (nếu chưa có):**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. **Cài đặt các gói phụ thuộc:**
   ```powershell
   pip install -r requirements.txt
   ```

3. **Khởi chạy Herdr Server (chạy ở một terminal riêng nếu chưa chạy):**
   ```powershell
   herdr server
   # Hoặc mở giao diện làm việc thông thường:
   herdr
   ```

4. **Khởi chạy Telegram Bridge:**
   ```powershell
   .\.venv\Scripts\python bridge.py
   # hoặc nếu đã active venv:
   python bridge.py
   ```

---

### Cách 2: Chạy bằng Docker (Dành cho Linux/Server)

```bash
docker compose up -d --build
```

---

## 📱 Danh sách lệnh trên Telegram

| Lệnh | Ý nghĩa | Ví dụ |
| :--- | :--- | :--- |
| `/start`, `/help` | Xem danh sách lệnh và hướng dẫn | `/help` |
| `/status` | Kiểm tra trạng thái Herdr Server & Client dạng Dashboard | `/status` |
| `/workspaces` | Liệt kê các workspaces dạng thẻ trực quan (Active, Tabs, Panes) | `/workspaces` |
| `/agents` | Liệt kê AI Agents kèm nút Duyệt/Từ chối và Xem Log trực tiếp | `/agents` |
| `/logs <pane_id>` | Xem 25 dòng log mới nhất của pane (đã lọc sạch mã ANSI) | `/logs w1:p2` |
| `/prompt <agent> <msg>` | Gửi prompt mới cho agent đang chạy | `/prompt reviewer Kiểm tra lại code` |
| `/stop <pane_id/agent>` | Dừng tiến trình/hủy tác vụ bằng tín hiệu Ctrl+C | `/stop w1:p2` |

