# Herdr Telegram Bridge

Điều khiển và theo dõi Herdr qua Telegram trên **Windows, Python 3.10+**.
Bot hỗ trợ thông báo blocked/hoàn thành, reply vào pane, lựa chọn y/n hoặc số,
điều hướng TUI, đọc log, giao prompt và gửi Ctrl+C.

## Cài đặt và khởi chạy

Cài Herdr CLI và khởi động Herdr riêng trước khi chạy bridge. Bridge không tự
khởi động, dừng hoặc cập nhật Herdr.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Điền token BotFather và Telegram User ID của bạn vào .env.
.\.venv\Scripts\python.exe bridge.py
```

`.env` được nạp từ thư mục dự án, không phụ thuộc thư mục làm việc. Biến môi
trường đã tồn tại được ưu tiên. Thiếu token, allowlist, CLI hoặc cấu hình không
hợp lệ sẽ khiến chương trình dừng trước khi kết nối Telegram.

Có thể chạy nền bằng `run_bridge_background.vbs`; script tự tìm `.venv` và
`bridge.py` cạnh chính nó. Khi cần xem lỗi khởi động, chạy lệnh Python trực tiếp.
File `.lnk` cũ là tiện ích riêng của máy tạo ra nó, không phải launcher di động.

## Cấu hình

| Biến | Mặc định / ý nghĩa |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bắt buộc, token của bot |
| `ALLOWED_TELEGRAM_USER_IDS` | Bắt buộc, ID người được điều khiển, cách nhau bằng dấu phẩy |
| `NOTIFICATION_CHAT_IDS` | ID chat bổ sung; **cộng với** các user ID trong allowlist, không thay thế |
| `HERDR_EXECUTABLE` | `herdr`, tìm trong PATH; có thể dùng đường dẫn đầy đủ tới `.exe` |
| `HERDR_COMMAND_TIMEOUT_SEC` | `15`, timeout cho mỗi lệnh CLI |
| `RATE_LIMIT_MAX_CALLS` / `RATE_LIMIT_PERIOD_SEC` | `15` thao tác / `60` giây cho mỗi người |
| `AUTO_NOTIFY_BLOCKED` / `AUTO_NOTIFY_IDLE` | `true` / `true` |
| `POLL_INTERVAL_SEC` | `3` giây; không chạy monitor chồng nhau |
| `TELEGRAM_CONNECT_TIMEOUT` / `TELEGRAM_READ_TIMEOUT` | `30` / `30` giây |
| `TELEGRAM_WRITE_TIMEOUT` / `TELEGRAM_POOL_TIMEOUT` | `30` / `15` giây |
| `TELEGRAM_PROXY` | Proxy tùy chọn; sau đó fallback `HTTPS_PROXY`, `HTTP_PROXY` |

Các timeout, chu kỳ và giới hạn phải dương và hữu hạn. ID user phải dương;
ID nhóm nhận thông báo có thể âm. Các biến boolean nhận `true/false`, `1/0`,
`yes/no`. Thư viện CLI tiếp tục nhận các biến Herdr, ví dụ `HERDR_SOCKET_PATH`,
từ môi trường của tiến trình.

## Lệnh Telegram

| Lệnh | Công dụng |
|---|---|
| `/start`, `/help` | Hướng dẫn |
| `/status`, `/workspaces` | Trạng thái và workspace |
| `/agents` hoặc nút **Agents** | Danh sách ngắn: tên phiên — loại agent — trạng thái |
| `/agents "Backend API"` hoặc `/agents w3:p1` | Mở thẻ điều khiển của một phiên |
| `/logs w3:p1` | Đọc 30 dòng output; nội dung dài được chia tin nhắn |
| `/prompt "Backend API" nhiệm vụ` hoặc `/prompt w3:p1 nhiệm vụ` | Giao việc theo tên phiên hoặc pane |
| `/rename w3:p1 Backend API` | Đặt tên riêng cho phiên |
| `/rename w3:p1 --clear` | Xóa tên riêng, dùng lại nhãn mặc định |
| `/reply w3:p1 nội dung` | Gửi nội dung rồi Enter |
| `/reply y` | Chỉ tự chọn khi có đúng một agent blocked |
| `/key w3:p1 up` | Gửi `enter`, `esc`, `up`, `down`, `space`, `tab`, `ctrl+c` |
| `/key enter` | Chỉ tự chọn khi có đúng một agent blocked |
| `/stop w3:p1` hoặc `/stop agent_name` | Gửi Ctrl+C |

Reply trực tiếp thông báo còn hiệu lực để gửi nội dung tới pane của thông báo.
Tin nhắn tự do không reply chỉ được chuyển tiếp khi có đúng một agent blocked;
không tự gửi vào agent idle. Muốn giao việc cho agent idle, dùng reply một
thông báo mới hoặc `/prompt`.

## Phân biệt nhiều phiên Codex

`/agents` và nút **Agents** chỉ trả danh sách ngắn, không đọc log terminal hay
gửi hàng loạt thẻ:

```text
Backend API — codex — ⚙️ Đang làm việc
Frontend — codex — 🔵 Rảnh rỗi
```

Dùng `/agents "Backend API"` hoặc `/agents w3:p1` khi cần mở đầy đủ nút điều
khiển, đổi tên, giao việc hoặc đọc log của một phiên. Tên mặc định bị trùng sẽ
được bổ sung pane ID trong danh sách ngắn để dễ phân biệt.

Mỗi thẻ hiển thị **tên phiên**, loại agent (ví dụ Codex), tên dự án, nhãn
workspace, pane và tác vụ hiện tại. Khi chưa đặt tên, bot lấy tên thư mục dự án
kèm loại agent làm nhãn mặc định; tên riêng do Herdr cung cấp được ưu tiên hơn
nhãn mặc định nếu có.

Đặt tên từ Telegram:

```text
/rename w3:p1 Backend API
/rename w3:p2 Frontend
/prompt "Backend API" Kiểm tra đăng nhập OAuth
/stop Frontend
```

Tên nhiều từ trong vị trí đích cần đặt trong dấu ngoặc kép. Tên tối đa 80 ký tự,
không chứa xuống dòng/ký tự điều khiển. Cũng có thể bấm **✏️ Đổi tên** và reply
thông báo yêu cầu nhập tên; nội dung reply này chỉ được dùng để đặt tên.
Nút **💬 Giao việc** mở thông báo reply riêng để gửi prompt vào đúng pane.

Tên được lưu cục bộ trong `data/agent_names.json`, gắn với `terminal_id` của
Herdr, được giữ qua lần khởi động lại bridge và khi terminal chuyển pane.
Terminal mới dùng lại cùng pane ID sẽ không nhận tên cũ. Với bản Herdr không
có `terminal_id`, khóa dự phòng gồm workspace, pane, loại agent và thư mục.
File tên phiên được ghi nguyên tử và không đưa vào Git/build context; hãy sao
lưu file này nếu cần chuyển cấu hình sang máy khác.

`/prompt`, `/stop` và `/rename` nhận tên riêng hoặc pane ID. Nếu tên trùng hoặc
chỉ gõ `/prompt codex ...` khi có nhiều Codex, bot hiển thị các thẻ để chọn đúng
phiên, **chưa thực hiện lệnh** trước khi bạn chọn. Mỗi yêu cầu chọn chỉ dùng một
lần; bot luôn kiểm tra đúng terminal trước khi thực hiện. Mọi lệnh gửi
prompt/phím được định tuyến bằng pane ID sau khi kiểm tra lại terminal.

## Quy tắc an toàn và xử lý lỗi

- Mọi lệnh, reply và nút đều kiểm tra allowlist và rate limit.
- Metadata gắn với cả chat và message ID. Không suy đoán pane từ chữ trong
  tin nhắn reply. Reply không rõ nguồn yêu cầu chỉ định pane bằng lệnh.
- Mọi nút kiểm tra đúng terminal và có hạn 24 giờ; pane đóng/thay terminal hoặc
  bot khởi động lại sẽ làm hết hiệu lực các nút điều khiển cũ. Xem log, mở menu,
  đổi tên, giao nhiệm vụ mới và điều hướng dùng trạng thái hiện tại, không bị
  chặn chỉ vì agent chuyển working/idle/blocked.
- Lựa chọn theo ngữ cảnh và Enter phải khớp câu hỏi đang hiển thị. Nếu câu hỏi
  đã đổi, bot cập nhật màn hình/bàn phím và yêu cầu bấm lại; không tự gửi xác nhận.
  Mỗi lần cập nhật dùng token mới để click cũ không tác động vào menu mới.
  Khi CLI thiếu số phiên trạng thái, xác nhận theo ngữ cảnh hết hạn sau 5 phút.
- Metadata giữ tối đa 10.000 tin nhắn. Nút hết hạn được làm mới bằng `/agents <pane_id>`.
- Mỗi pane chỉ nhận một thao tác ghi tại một thời điểm. Gửi chữ thất bại sẽ
  không gửi Enter; gửi chữ thành công nhưng Enter lỗi sẽ báo riêng.
- Gửi text, phím và prompt không được tự retry: timeout có thể có nghĩa lệnh
  đã chạy. Kiểm tra pane trước khi chủ động gửi lại. Nút xác nhận đã dùng hoặc
  có kết quả không chắc chắn sẽ không được dùng lại; các bản thông báo cùng
  trạng thái ở những chat khác cũng bị vô hiệu hóa cho thao tác ghi.
- Thông báo được retry riêng từng chat tối đa 3 lần sau lần gửi đầu, chờ
  3/10/30 giây cho lỗi mạng hoặc theo `RetryAfter` của Telegram. Thời điểm thực
  tế phụ thuộc chu kỳ monitor. Lỗi chat bị chặn/cấu hình sai không retry.
  Sự kiện đã bị trạng thái mới thay thế sẽ bị hủy; hết lượt retry được ghi log.
- Không lưu metadata/hàng đợi qua restart. Khi khởi động lại, chỉ thông báo
  agent đang blocked; không phát lại thông báo hoàn thành cũ.
- Audit ghi loại hành động, user, pane, độ dài và kết quả, không ghi nguyên
  prompt hoặc token. Log nằm ở `logs/audit_bridge.log`, tối đa 5 file × 10 MB.
  Nội dung terminal vẫn được gửi tới các chat nhận thông báo đã cấu hình.

## Phát triển và kiểm thử

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
```

`bridge.py` là entrypoint. Package `herdr_bridge` tách cấu hình, CLI client,
state/metadata, tên phiên, monitor, render HTML và Telegram handlers. Import module không
nạp `.env`, tạo log hay kết nối mạng. Dependency runtime và development được
chốt phiên bản; CI chạy kiểm tra trên Windows với Python 3.10 và 3.12.

Kiểm thử tự động dùng CLI/Telegram giả lập, cộng một subprocess Python kiểm
tra timeout; không gửi phím tới Herdr thật. Trước khi dùng bản mới, dừng bridge
cũ rồi chạy bản mới với **một pane thử nghiệm**, kiểm tra `/status`, `/agents`,
`/logs`, `/prompt`, reply, TUI và thông báo blocked → working → idle. Thử nút
cũ sau restart: bot phải yêu cầu làm mới. Chỉ chạy một instance cho mỗi token.

Khi rollback, dừng tiến trình mới trước khi chạy lại bản cũ. Các nút mới không
tương thích với bản cũ; dùng `/agents` để tạo lại giao diện điều khiển.

## Docker và cấu hình tham khảo

**Docker Linux chưa được hỗ trợ hoặc kiểm chứng chính thức.** `Dockerfile`
và Compose hiện chỉ là cấu hình tham khảo: image chưa cài Herdr CLI và đường
dẫn socket phụ thuộc môi trường host. Không dùng `docker compose up` làm quy
trình triển khai trong phiên bản này. `.dockerignore` loại secrets, môi trường
ảo, Git và logs khỏi build context.

`ccgram-herdr-config.yaml` là ví dụ tích hợp CCGram riêng; bridge Python không
đọc file này.
