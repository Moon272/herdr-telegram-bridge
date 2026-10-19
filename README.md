# Herdr Telegram Bridge 🚀

Cổng điều khiển và giám sát từ xa an toàn cho **Herdr** (AI Coding Agent Workspace Manager) qua **Telegram Bot**.

---

## 📌 Tính năng chính

- **Tự động bắt chuyển tiếp trạng thái & thông báo hoàn thành (Working ➔ Idle / Completion Push)**:
  - Tự động phát hiện chính xác thời điểm agent kết thúc lượt làm việc (chuyển tiếp trạng thái từ **Working** sang **Idle / Rảnh rỗi** hoặc **Done**).
  - Tự động trích xuất kết quả / 15 dòng output cuối cùng từ terminal pane và gửi thông báo tức thời lên Telegram để người dùng không bỏ lỡ thời điểm agent hoàn tất nhiệm vụ.
  - Tích hợp sẵn nút tương tác nhanh: `💬 Giao nhiệm vụ tiếp theo`, `📜 Xem 30 dòng Log`, `🔄 Trạng thái Agents`.
  - Hỗ trợ vuốt **Reply** trực tiếp tin nhắn hoàn thành để giao chỉ thị tiếp theo vào terminal pane ngay lập tức.
- **Tự động gửi thông báo phê duyệt & điều khiển lựa chọn (Proactive Approval & Choices Push)**:
  - Tự động theo dõi các AI Agents theo thời gian thực (background loop).
  - Khi agent chuyển sang trạng thái **Blocked** (chờ xác nhận lệnh, sửa file, câu hỏi y/n, hoặc menu lựa chọn), bot sẽ **tự động gửi tin nhắn báo động lên Telegram** kèm trích xuất ngữ cảnh log gần nhất.
  - **Tự động nhận diện lựa chọn (Dynamic Choice Extraction)**: Tự động phân tích các menu đánh số (`1️⃣, 2️⃣...`), phím tắt chữ cái (`[Y] Yes, [N] No, [A] All, [D] Diff...`) hoặc định dạng slash (`[y/n/d]`) và tạo các nút bấm chọn 1 chạm trực tiếp trên Telegram.
  - **🎮 Bàn phím điều hướng TUI tương tác (Arrow Navigation)**: Điều khiển menu mũi tên (`⬆️ Lên`, `⬇️ Xuống`, `␣ Space` đánh dấu, `⏎ Enter` chọn, `⎋ Esc` hủy) với màn hình terminal cập nhật theo thời gian thực ngay trên tin nhắn Telegram.
  - **🔢 Phím số nhanh**: Bàn phím số 1️⃣ đến 5️⃣ cho các câu hỏi chọn nhanh.
  - **Tương tác linh hoạt qua Reply & Text**: Người dùng có thể vuốt **Reply** trực tiếp tin nhắn thông báo hoặc gửi tin nhắn văn bản khi agent đang chờ để gửi bất kỳ input nào vào terminal pane.
- **Kiểm tra trạng thái hệ thống**: Giám sát Herdr Server & Client trực tiếp từ Telegram (`/status`).
- **Quản lý Workspaces & AI Agents**: Liệt kê các workspace đang hoạt động (`/workspaces`), danh sách các AI Agents (`/agents`) kèm các nút Duyệt / Từ chối / Điều khiển.
- **Xem Log thời gian thực**: Trích xuất log gần nhất của từng pane (`/logs <pane_id>`).
- **Gửi chỉ thị (Prompt Injection)**: Gửi prompt/chỉ thị mới tới agent từ xa (`/prompt <agent_name> <nội dung>`).
- **Gửi input tùy ý vào terminal**: Điều khiển terminal trực tiếp bằng lệnh (`/reply [pane_id] <nội dung>`). Tự động nhận diện agent đang chờ nếu bỏ qua pane_id.
- **Gửi phím điều hướng**: Điều khiển bàn phím terminal qua lệnh (`/key [pane_id] <phím>`).
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

# Tự động gửi thông báo phê duyệt khi agent bị blocked
AUTO_NOTIFY_BLOCKED=true
# Tự động gửi thông báo khi agent hoàn thành công việc (chuyển sang rảnh rỗi)
AUTO_NOTIFY_IDLE=true
POLL_INTERVAL_SEC=3
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
| `/agents` | Liệt kê AI Agents kèm nút Duyệt/Từ chối/Điều khiển và Xem Log trực tiếp | `/agents` |
| `/logs <pane_id>` | Xem 30 dòng log mới nhất của pane (đã lọc sạch mã ANSI) | `/logs w3:p1` |
| `/prompt <agent> <msg>` | Gửi prompt mới cho agent đang chạy | `/prompt agy Kiểm tra lại code` |
| `/reply [pane_id] <msg>` | Gửi nội dung kèm Enter trực tiếp vào pane terminal (tự nhận diện agent đang chờ) | `/reply 1` hoặc `/reply w3:p1 y` |
| `/key [pane_id] <phím>` | Gửi phím điều hướng terminal (`enter`, `esc`, `up`, `down`, `space`, `tab`) | `/key enter` hoặc `/key w3:p1 up` |
| `/stop <pane_id/agent>` | Dừng tiến trình/hủy tác vụ bằng tín hiệu Ctrl+C | `/stop w3:p1` |
