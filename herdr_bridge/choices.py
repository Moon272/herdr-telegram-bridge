import re
from typing import Dict, List, Tuple


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
        any(cursor in line for cursor in ("❯", "›", "●", "->", "=>"))
        or any(
            kw in line.lower()
            for kw in ("arrow keys", "select an option", "choose one", "press enter", "select option")
        )
        for line in lines
    )

    # 1. Quét các lựa chọn đánh số: 1) hoặc 1. hoặc [1] hoặc (1) hoặc ❯ 1)
    num_pattern = re.compile(r"^(?:[❯>•\-\*]\s*)?(?:\[(\d+)\]|\((\d+)\)|(\d+)[\.\:\)])\s*(.+)$")
    for line in lines:
        m = num_pattern.match(line)
        if m:
            val = m.group(1) or m.group(2) or m.group(3)
            raw_label = m.group(4).strip()
            # Bỏ qua nếu label quá dài (có thể là code/log ngẫu nhiên) hoặc quá ngắn
            if 1 <= len(raw_label) <= 80:
                short_label = raw_label[:20] + "…" if len(raw_label) > 20 else raw_label
                emoji_digits = {
                    "1": "1️⃣",
                    "2": "2️⃣",
                    "3": "3️⃣",
                    "4": "4️⃣",
                    "5": "5️⃣",
                    "6": "6️⃣",
                    "7": "7️⃣",
                    "8": "8️⃣",
                    "9": "9️⃣",
                }
                emoji = emoji_digits.get(val, f"[{val}]")
                choices.append({"key": val, "label": f"{emoji} {short_label}", "raw": raw_label})

    # 2. Nếu không có số, kiểm tra các phím tắt chữ cái kiểu (y)es, (n)o, (a)lways, (d)iff, (e)dit
    if not choices:
        letter_pattern = re.compile(r"\(([a-zA-Z0-9])\)\s*([a-zA-Z0-9_\-]{1,15})")
        for line in lines:
            matches = letter_pattern.findall(line)
            if len(matches) >= 2:
                for key, word in matches:
                    full_word = f"{key}{word}".capitalize()
                    choices.append(
                        {"key": key, "label": f"[{key.upper()}] {full_word}", "raw": f"({key}) {word}"}
                    )
                break

    # 3. Kiểm tra định dạng slash kiểu [y/n/a/d/q]
    if not choices:
        slash_match = re.search(r"\[([a-zA-Z]/[a-zA-Z/]+)\]", text)
        if slash_match:
            parts = slash_match.group(1).split("/")
            for p in parts:
                p = p.strip()
                if p:
                    choices.append({"key": p, "label": f"[{p.upper()}]", "raw": p})

    return choices[:6], is_tui
