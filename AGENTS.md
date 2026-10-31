# Hướng dẫn làm việc trong repository

Áp dụng cho toàn bộ repository. Giao tiếp với người dùng bằng tiếng Việt, trừ
khi có yêu cầu khác. Đọc `README.md` và mã liên quan trước khi thay đổi; bảo toàn
các thay đổi cục bộ đã có trong working tree.

## Dự án và cấu trúc

Đây là bridge Python điều khiển, giám sát Herdr qua Telegram, dùng long polling
và job nền. Môi trường hỗ trợ chính là Windows, Python 3.10+; CI kiểm tra trên
Windows với Python 3.10 và 3.12. Docker hiện là cấu hình tham khảo chưa được hỗ trợ
chính thức. `ccgram-herdr-config.yaml` là ví dụ riêng, không được bridge Python đọc.

| Thành phần | Trách nhiệm |
| --- | --- |
| `bridge.py` | Entrypoint tương thích `python bridge.py` |
| `herdr_bridge/app.py` | Telegram handlers, định tuyến, ghép dependency |
| `herdr_bridge/client.py` | Herdr CLI, timeout, khóa thao tác theo pane |
| `herdr_bridge/state.py` | Agent snapshot, metadata, kiểm tra nút, chuyển trạng thái |
| `herdr_bridge/names.py` | Tên phiên lưu cục bộ, ghi file nguyên tử |
| `herdr_bridge/monitor.py` | Phát hiện sự kiện, hàng đợi và retry từng chat |
| `herdr_bridge/render.py`, `choices.py` | HTML, bàn phím, nhận diện lựa chọn terminal |
| `herdr_bridge/config.py` | Cấu hình lúc khởi động và logging |
| `tests/` | Kiểm thử hồi quy và CLI integration tùy môi trường |

Giữ import module không tạo file/log, nạp `.env`, đổi console hay kết nối mạng.
Tách xử lý CLI, trạng thái và giao diện theo các module trên. Giữ tương thích
Python 3.10 và cập nhật tài liệu khi thay đổi hành vi người dùng nhìn thấy.

## Cài đặt và kiểm tra

Chạy từ thư mục gốc bằng PowerShell. Ưu tiên interpreter của `.venv`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Nếu chưa có môi trường ảo: `python -m venv .venv`. Phiên bản dependency được chốt
trong `requirements.txt` và `requirements-dev.txt`; cập nhật nhất quán khi cần.
Ruff dùng cấu hình trong `pyproject.toml`, chiều dài dòng 110.

Với thay đổi hành vi, thêm kiểm thử hồi quy tái hiện lỗi và chạy các kiểm tra
liên quan; trước bàn giao thay đổi runtime, chạy các kiểm tra CI ở trên. Với thay
đổi chỉ tài liệu, kiểm tra nội dung, đường dẫn và diff là đủ.

Kiểm thử dùng mock Telegram/CLI và subprocess thử nghiệm. Các ca integration
Herdr dùng đích ngẫu nhiên không tồn tại để kiểm tra parser; bỏ qua khi thiếu
CLI/server. Không gửi nhiệm vụ, phím hoặc tin nhắn thử tới agent/chat đang dùng
thật chỉ để làm kiểm thử tự động thành công. Báo rõ phần nào đã chạy, bị skip
hoặc chưa được kiểm chứng thực tế.

## Các hành vi cần giữ

- `/agents` và nút **Agents** chỉ trả danh sách ngắn: tên dễ đọc — loại agent —
  trạng thái. Luồng này không đọc output terminal hoặc gửi hàng loạt thẻ lớn.
- `/agents <pane_id>` hoặc `/agents "Tên phiên"` mở thẻ điều khiển một phiên.
  Khi tên nhiều từ ở vị trí đích, hỗ trợ dấu ngoặc kép và giữ nguyên nội dung
  prompt phía sau, gồm dấu ngoặc kép và xuống dòng.
- Tách tên phiên khỏi loại agent như `codex`/`claude`. Nhiều pane có thể chạy cùng
  loại agent. Tên trùng phải yêu cầu chọn phiên, chưa thực thi lệnh trước khi chọn.
- Tên riêng được lưu ở `data/agent_names.json`, ưu tiên khóa `terminal_id`.
  Đổi tên không gửi text/phím vào terminal. Giữ ghi file nguyên tử và tên cũ khi
  ghi thất bại. Tên hiển thị không tham gia định danh an toàn của phiên.
- Gửi prompt/phím bằng pane ID sau khi xác định đích và kiểm tra lại terminal.
  Không định tuyến lệnh ghi chỉ bằng chuỗi loại agent như `codex`.
- Nút **Log** đọc 30 dòng gần nhất. Output sẵn trên thẻ đọc 15 dòng, phần snippet
  hiển thị giới hạn 1.800 đơn vị UTF-16. Nội dung dài được chia tin nhắn.

## An toàn khi điều khiển và quản lý trạng thái

- Mọi command, callback và reply phải kiểm tra allowlist và rate limit. Cấu hình
  thiếu token/allowlist phải dừng sớm. Chat nhận thông báo không tự có quyền điều khiển.
- Metadata tin nhắn dùng khóa `(chat_id, message_id)`. Kiểm tra nguồn reply và
  đúng terminal; không suy đoán pane bằng regex trên chữ trong tin nhắn.
- Metadata có TTL và giới hạn dung lượng. Giữ kiểm tra terminal bị đóng hoặc
  thay thế, request đã xử lý, callback lặp và lựa chọn chỉ dùng một lần trong
  cả nhóm thẻ chọn đích.
- Xem log, mở menu, đổi tên, nhập nhiệm vụ mới và điều hướng kiểm tra terminal
  hiện tại. Không chặn các thao tác này chỉ vì số phiên/trạng thái agent thay đổi.
- Lựa chọn theo ngữ cảnh, reply text theo ngữ cảnh và Enter cần kiểm tra snapshot
  phù hợp. Với callback đã đổi ngữ cảnh, cập nhật bàn phím và yêu cầu bấm lại;
  không tự gửi xác nhận vào câu hỏi mới.
- Mỗi lần cập nhật bàn phím tạo token mới và bind metadata sau khi Telegram edit
  thành công. Click trễ của menu cũ không được tác động lên menu mới.
- Tuần tự hóa thao tác ghi theo pane; kiểm tra đích bên trong khóa. Chỉ gửi Enter
  sau khi gửi text thành công. Phân biệt lỗi chưa gửi chữ với lỗi đã gửi chữ nhưng
  Enter thất bại. Không fallback sang hành động khác hoặc tự retry lệnh ghi.
- Herdr CLI nhận nội dung ở đối số vị trí. **Không chèn `--` trước TEXT** trong
  `agent prompt` hoặc `pane send-text`; `agent prompt` sẽ coi TEXT tiếp theo là
  tùy chọn sai. Gọi subprocess bằng danh sách đối số, không qua shell.
- Lệnh CLI phải có timeout và thu hồi tiến trình khi timeout/hủy. Không ghi raw
  prompt hay stderr có thể chứa prompt vào audit; ưu tiên mã lỗi đã chuẩn hóa.
- Monitor không chạy chồng. Lỗi đọc CLI không được coi là snapshot rỗng. Phát
  hiện hoàn thành phải xét cả `completion_seq`, độc lập với `state_change_seq`.
- Retry thông báo theo từng chat, có giới hạn và tôn trọng `RetryAfter`; hủy sự
  kiện lỗi thời. Trong PTB, `BadRequest` kế thừa `NetworkError`: bắt lỗi vĩnh viễn
  trước nhánh retry lỗi mạng. Lỗi gửi tin xác nhận không được phát lại lệnh terminal.
- Escape dữ liệu động trước khi render HTML, loại ANSI và tính giới hạn Telegram
  theo đơn vị UTF-16. Trạng thái chưa biết giữ là `unknown`, không suy diễn bằng
  cách tìm chuỗi con.

## Cấu hình, secrets và vận hành Windows

`.env` được nạp từ gốc dự án; biến môi trường hiện có được ưu tiên. Trên máy đang
dùng, token và allowlist có thể nằm trong biến môi trường Windows cấp User.
Shell đã mở trước khi đặt biến có thể chưa nhận giá trị mới; khi cần khởi chạy,
nạp các biến cấu hình liên quan vào môi trường tiến trình con và không in giá trị.

Không đưa token, thông tin xác thực proxy, `.env`, `.venv`, `logs/` hoặc `data/`
vào Git/build context. Không đọc hoặc in toàn bộ log/môi trường chỉ để kiểm tra
khởi động; ưu tiên mã lỗi, PID và các marker trạng thái. Audit xoay vòng được
cấu hình trong `config.py`; không bật log HTTP chứa URL token.

Khi yêu cầu công việc cần khởi chạy lại bridge:

1. Xác định đúng tiến trình bằng executable của `.venv` trong repo và command line
   `bridge.py`. Windows có thể hiện cặp Python cha/con cho một instance.
2. Kiểm tra cấu hình trước khi dừng bản đang chạy. Chỉ dừng đúng instance của repo,
   không dừng toàn bộ Python, Herdr hoặc các agent của người dùng.
3. Khởi chạy với working directory là repo và cửa sổ ẩn. Dùng launcher
   `run_bridge_background.vbs` hoặc `Start-Process -WindowStyle Hidden`.
4. Kiểm tra tiến trình còn chạy, marker `Application started`/`Scheduler started`,
   lỗi xác thực, mạng và xung đột polling. Mỗi token chỉ chạy một instance bridge.
5. Metadata điều khiển không lưu qua restart; hướng dẫn mở thẻ mới bằng
   `/agents <pane_id>` khi cần. Tên phiên riêng vẫn được lưu trên đĩa.

Launcher `.vbs` lấy đường dẫn theo vị trí của chính nó. Giữ cách này thay vì gắn
đường dẫn máy cá nhân vào mã. Shortcut Startup trỏ tới launcher trong repo;
file `.lnk` có sẵn là tiện ích riêng của máy tạo nó.
