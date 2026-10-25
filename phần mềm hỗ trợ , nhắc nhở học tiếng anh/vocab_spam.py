"""
Vocab Spam - Học từ vựng bằng popup nhắc liên tục + bài test cuối ngày.
Python 3.9+ | Tkinter + SQLite (thư viện chuẩn, không cần pip install gì thêm).

Chạy thử:        python vocab_spam.py
Chạy ngầm:       pythonw vocab_spam.py --background
Đóng gói .exe:   pyinstaller --onefile --noconsole --name VocabSpam vocab_spam.py
"""
import difflib
import os
import random
import re
import socket
import sqlite3
import sys
import threading
import unicodedata
from datetime import date, datetime, time as dtime, timedelta

import tkinter as tk
from tkinter import messagebox, ttk

from library_seed import SEED_TEXT

APP_NAME = "VocabSpam"
DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
DB_PATH = os.path.join(DATA_DIR, "vocab.db")
EXTRA_LIBRARY = os.path.join(DATA_DIR, "library.txt")  # thêm từ cho LibraryWords
LOCK_PORT = 47653          # chống chạy 2 bản cùng lúc
TICK_MS = 15_000           # bộ đếm giờ kiểm tra mỗi 15 giây
TEST_RETRY_MIN = 30        # nếu thoát giữa chừng bài test, 30 phút sau hỏi lại
SPAM_UNLOCK_SECONDS = 15   # sau chừng này giây mới hiện nút "Tôi đã nhớ"

BG, FG, ACCENT = "#1e1e2e", "#cdd6f4", "#89b4fa"
GOOD, BAD, BTN = "#a6e3a1", "#f38ba8", "#313244"

DEFAULTS = {"interval_min": "60", "words_per_day": "10", "test_time": "21:00"}


# ───────────────────────── Tiện ích xử lý chuỗi ─────────────────────────
def norm(s: str) -> str:
    """Chuẩn hoá: thường, bỏ dấu tiếng Việt, bỏ ký tự đặc biệt."""
    s = unicodedata.normalize("NFD", s.lower().replace("đ", "d"))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_correct(user_text: str, meaning: str) -> bool:
    """Đúng nếu khớp (không phân biệt dấu/hoa thường) với bất kỳ nghĩa nào,
    cho phép sai chính tả nhẹ (độ giống >= 85%)."""
    u = norm(user_text)
    if not u:
        return False
    candidates = [norm(p) for p in re.split(r"[,;/|]", meaning)] + [norm(meaning)]
    for c in filter(None, candidates):
        if u == c:
            return True
        if len(u) >= 3 and difflib.SequenceMatcher(None, u, c).ratio() >= 0.85:
            return True
    return False


_SEP = re.compile(r"\s+[-–—]\s+|\s*[–—]\s*|\t+|\s*[:=]\s*")


def parse_lines(text: str):
    """'Từ_mới - Nghĩa' (mỗi dòng một từ) -> ([(word, meaning)], [dòng lỗi])."""
    pairs, bad = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = _SEP.split(line, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            pairs.append((parts[0].strip(), parts[1].strip()))
        else:
            bad.append(line)
    return pairs, bad


def parse_hhmm(s: str, default=dtime(21, 0)) -> dtime:
    try:
        h, m = s.strip().split(":")
        return dtime(int(h), int(m))
    except Exception:
        return default


# ───────────────────────────── Cơ sở dữ liệu ─────────────────────────────
class Database:
    def __init__(self, path: str = DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._create()
        self._seed_library()

    def _create(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS UserWords(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL UNIQUE COLLATE NOCASE,
                meaning TEXT NOT NULL,
                added_at TEXT NOT NULL,
                is_learned INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS LibraryWords(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL UNIQUE COLLATE NOCASE,
                meaning TEXT NOT NULL,
                is_learned INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS DailyWords(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                source TEXT NOT NULL,          -- 'user' | 'library'
                word_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                meaning TEXT NOT NULL,
                spam_count INTEGER NOT NULL DEFAULT 0,
                tested INTEGER NOT NULL DEFAULT 0,
                is_correct INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS Settings(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        self.conn.commit()

    def _seed_library(self):
        pairs, _ = parse_lines(SEED_TEXT)
        if os.path.exists(EXTRA_LIBRARY):
            with open(EXTRA_LIBRARY, encoding="utf-8") as f:
                extra, _ = parse_lines(f.read())
            pairs += extra
        self.conn.executemany(
            "INSERT OR IGNORE INTO LibraryWords(word, meaning) VALUES(?,?)", pairs
        )
        self.conn.commit()

    # ---- cài đặt ----
    def get(self, key, default=None):
        r = self.conn.execute("SELECT value FROM Settings WHERE key=?", (key,)).fetchone()
        if r:
            return r["value"]
        return DEFAULTS.get(key, default)

    def get_int(self, key, default):
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def set(self, key, value):
        self.conn.execute(
            "INSERT INTO Settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    # ---- nhập từ ----
    def add_user_words(self, text: str):
        pairs, bad = parse_lines(text)
        added = updated = 0
        now = datetime.now().isoformat(timespec="seconds")
        for word, meaning in pairs:
            exists = self.conn.execute(
                "SELECT 1 FROM UserWords WHERE word=?", (word,)
            ).fetchone()
            self.conn.execute(
                "INSERT INTO UserWords(word, meaning, added_at) VALUES(?,?,?) "
                "ON CONFLICT(word) DO UPDATE SET meaning=excluded.meaning, is_learned=0",
                (word, meaning, now),
            )
            updated += bool(exists)
            added += not exists
        self.conn.commit()
        return added, updated, bad

    # ---- thuật toán lọc từ đầu ngày ----
    def prepare_today(self):
        """Đủ N từ -> lấy từ UserWords; thiếu X từ -> bù X từ ngẫu nhiên từ LibraryWords.
        Gọi lại nhiều lần an toàn: chỉ bổ sung phần còn thiếu của hôm nay."""
        today = date.today().isoformat()
        n = self.get_int("words_per_day", 10)
        have = self.conn.execute("SELECT source, word_id FROM DailyWords WHERE day=?", (today,)).fetchall()
        missing = n - len(have)
        if missing <= 0:
            return
        used_u = {r["word_id"] for r in have if r["source"] == "user"}
        used_l = {r["word_id"] for r in have if r["source"] == "library"}

        users = [
            r for r in self.conn.execute(
                "SELECT id, word, meaning FROM UserWords WHERE is_learned=0 ORDER BY added_at, id"
            ) if r["id"] not in used_u
        ][:missing]
        picked = [("user", r) for r in users]

        need = missing - len(users)
        if need > 0:
            libs = [
                r for r in self.conn.execute(
                    "SELECT id, word, meaning FROM LibraryWords WHERE is_learned=0 ORDER BY RANDOM()"
                ) if r["id"] not in used_l
            ][:need]
            picked += [("library", r) for r in libs]

        self.conn.executemany(
            "INSERT INTO DailyWords(day, source, word_id, word, meaning) VALUES(?,?,?,?,?)",
            [(today, src, r["id"], r["word"], r["meaning"]) for src, r in picked],
        )
        self.conn.commit()

    # ---- popup spam ----
    def next_spam_word(self):
        return self.conn.execute(
            "SELECT * FROM DailyWords WHERE day=? ORDER BY spam_count ASC, RANDOM() LIMIT 1",
            (date.today().isoformat(),),
        ).fetchone()

    def bump_spam(self, daily_id):
        self.conn.execute("UPDATE DailyWords SET spam_count=spam_count+1 WHERE id=?", (daily_id,))
        self.conn.commit()

    # ---- bài test cuối ngày ----
    def pending_test_words(self):
        today = date.today().isoformat()
        spammed = self.conn.execute(
            "SELECT COUNT(*) c FROM DailyWords WHERE day=? AND spam_count>0", (today,)
        ).fetchone()["c"]
        cond = "AND spam_count>0" if spammed else ""
        return self.conn.execute(
            f"SELECT * FROM DailyWords WHERE day=? AND tested=0 {cond}", (today,)
        ).fetchall()

    def record_test(self, daily_id, ok: bool):
        row = self.conn.execute("SELECT * FROM DailyWords WHERE id=?", (daily_id,)).fetchone()
        self.conn.execute(
            "UPDATE DailyWords SET tested=1, is_correct=? WHERE id=?", (int(ok), daily_id)
        )
        if ok:
            table = "UserWords" if row["source"] == "user" else "LibraryWords"
            self.conn.execute(f"UPDATE {table} SET is_learned=1 WHERE id=?", (row["word_id"],))
        self.conn.commit()

    def distractors(self, meaning: str, k: int = 3):
        rows = self.conn.execute(
            "SELECT meaning FROM (SELECT meaning FROM LibraryWords "
            "UNION SELECT meaning FROM UserWords) ORDER BY RANDOM() LIMIT 40"
        ).fetchall()
        target, out = norm(meaning), []
        for r in rows:
            m = r["meaning"]
            if norm(m) != target and m not in out:
                out.append(m)
            if len(out) == k:
                break
        return out

    def stats(self):
        q = lambda sql, *a: self.conn.execute(sql, a).fetchone()[0]
        today = date.today().isoformat()
        return {
            "user_new": q("SELECT COUNT(*) FROM UserWords WHERE is_learned=0"),
            "user_done": q("SELECT COUNT(*) FROM UserWords WHERE is_learned=1"),
            "lib_left": q("SELECT COUNT(*) FROM LibraryWords WHERE is_learned=0"),
            "today": q("SELECT COUNT(*) FROM DailyWords WHERE day=?", today),
            "spammed": q("SELECT COUNT(*) FROM DailyWords WHERE day=? AND spam_count>0", today),
            "correct": q("SELECT COUNT(*) FROM DailyWords WHERE day=? AND is_correct=1", today),
        }


# ───────────────────────── Khởi động cùng Windows ─────────────────────────
def _run_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --background'
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(__file__)}" --background'


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_supported() -> bool:
    return sys.platform == "win32"


def is_startup_enabled() -> bool:
    if not startup_supported():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(enabled: bool):
    """Ghi vào HKCU\\...\\Run - tương đương bỏ shortcut vào shell:startup."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _run_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ───────────────────────────── Chỉ chạy 1 bản ─────────────────────────────
def acquire_lock(notify_existing: bool):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        if notify_existing:  # mở lại exe -> hiện cửa sổ của bản đang chạy ngầm
            try:
                c = socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=1)
                c.sendall(b"SHOW")
                c.close()
            except OSError:
                pass
        return None


# ──────────────────────────────── Popup spam ────────────────────────────────
class SpamPopup(tk.Toplevel):
    def __init__(self, app, row):
        super().__init__(app.root)
        self.app, self.row = app, row
        self.fails, self.left, self._closed = 0, SPAM_UNLOCK_SECONDS, False

        self.title("Từ vựng mới")
        self.configure(bg=BG)
        self.attributes("-topmost", True)          # đè lên mọi cửa sổ/game
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # không cho bấm X

        tk.Label(self, text="ĐẾN GIỜ HỌC TỪ MỚI", bg=BG, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(pady=(22, 0), padx=60)
        tk.Label(self, text=row["word"], bg=BG, fg=ACCENT,
                 font=("Segoe UI", 36, "bold")).pack(pady=(6, 10), padx=60)
        tk.Label(self, text="Gõ nghĩa tiếng Việt của từ này để đóng cửa sổ",
                 bg=BG, fg=FG, font=("Segoe UI", 10)).pack()

        self.var = tk.StringVar()
        self.entry = tk.Entry(self, textvariable=self.var, font=("Segoe UI", 16),
                              justify="center", width=28)
        self.entry.pack(pady=12)
        self.entry.bind("<Return>", lambda e: self.check())

        self.msg = tk.Label(self, text="", bg=BG, fg=FG, font=("Segoe UI", 11),
                            wraplength=420)
        self.msg.pack(pady=(0, 8))

        bar = tk.Frame(self, bg=BG)
        bar.pack(pady=(0, 20))
        self.ok_btn = tk.Button(bar, text="Kiểm tra", command=self.check, bg=BTN, fg=FG,
                                relief="flat", padx=16, pady=6, font=("Segoe UI", 10, "bold"))
        self.ok_btn.pack(side="left", padx=6)
        self.skip_btn = tk.Button(bar, text=f"Tôi đã nhớ ({self.left}s)", state="disabled",
                                  command=self._close, bg=BTN, fg=FG, relief="flat",
                                  padx=16, pady=6, font=("Segoe UI", 10))
        self.skip_btn.pack(side="left", padx=6)

        self._center()
        self.after(100, self._grab)
        self.after(1000, self._countdown)
        self.after(1000, self._keep_on_top)

    def _center(self):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x, y = (self.winfo_screenwidth() - w) // 2, (self.winfo_screenheight() - h) // 3
        self.geometry(f"+{x}+{y}")

    def _grab(self):
        try:
            self.lift()
            self.focus_force()
            self.entry.focus_set()
            self.grab_set()
        except tk.TclError:
            pass

    def _keep_on_top(self):
        if self._closed:
            return
        self.attributes("-topmost", True)
        self.lift()
        if self.focus_displayof() is None:   # bị cửa sổ khác cướp focus -> giành lại
            self.focus_force()
            self.entry.focus_set()
        self.after(1000, self._keep_on_top)

    def _countdown(self):
        if self._closed:
            return
        self.left -= 1
        if self.left > 0:
            self.skip_btn.config(text=f"Tôi đã nhớ ({self.left}s)")
            self.after(1000, self._countdown)
        else:
            self.skip_btn.config(text="Tôi đã nhớ rồi", state="normal")

    def check(self):
        if self._closed:
            return
        if is_correct(self.var.get(), self.row["meaning"]):
            self.msg.config(text=f"Chính xác!  {self.row['word']} = {self.row['meaning']}", fg=GOOD)
            self.entry.config(state="disabled")
            self.ok_btn.config(state="disabled")
            self.after(1200, self._close)
        else:
            self.fails += 1
            hint = ""
            if self.fails >= 2:
                first = re.split(r"[,;/|]", self.row["meaning"])[0].strip()
                hint = f"  Gợi ý: {first[:2]}…"
            self.msg.config(text="Chưa đúng, thử lại nhé." + hint, fg=BAD)
            self.entry.select_range(0, "end")

    def _close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.app.popup_closed()
        self.destroy()


# ─────────────────────────────── Bài test cuối ngày ───────────────────────────────
class TestWindow(tk.Toplevel):
    def __init__(self, app, rows):
        super().__init__(app.root)
        self.app, self.db = app, app.db
        self.rows = list(rows)
        random.shuffle(self.rows)
        self.i, self.correct = 0, 0
        self.answered, self.finished, self._closed = False, False, False

        self.title("Bài test ôn tập cuối ngày")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.geometry("580x500")

        self.head = tk.Label(self, bg=BG, fg=FG, font=("Segoe UI", 11, "bold"))
        self.head.pack(pady=(16, 0))
        self.word_lbl = tk.Label(self, bg=BG, fg=ACCENT, font=("Segoe UI", 30, "bold"))
        self.word_lbl.pack(pady=6)
        self.prompt = tk.Label(self, bg=BG, fg=FG, font=("Segoe UI", 10))
        self.prompt.pack()
        self.area = tk.Frame(self, bg=BG)
        self.area.pack(fill="x", padx=40, pady=10)
        self.fb = tk.Label(self, bg=BG, fg=FG, font=("Segoe UI", 11), wraplength=500)
        self.fb.pack(pady=4)
        self.next_btn = tk.Button(self, text="Câu tiếp theo", command=self._next, bg=BTN, fg=FG,
                                  relief="flat", padx=16, pady=6, state="disabled")
        self.next_btn.pack(pady=8)
        self.bind("<Return>", lambda e: self._next() if self.answered else None)

        x = (self.winfo_screenwidth() - 580) // 2
        y = (self.winfo_screenheight() - 500) // 3
        self.geometry(f"+{x}+{y}")
        self.after(100, lambda: (self.lift(), self.focus_force()))
        self._show()

    def _show(self):
        for w in self.area.winfo_children():
            w.destroy()
        row = self.rows[self.i]
        self.answered = False
        self.fb.config(text="")
        last = self.i == len(self.rows) - 1
        self.next_btn.config(state="disabled", text="Xem kết quả" if last else "Câu tiếp theo")
        self.head.config(text=f"Câu {self.i + 1}/{len(self.rows)}")
        self.word_lbl.config(text=row["word"])

        opts = self.db.distractors(row["meaning"], 3)
        if len(opts) == 3 and random.random() < 0.5:       # trắc nghiệm
            self.mode = "mc"
            self.prompt.config(text="Chọn nghĩa đúng:")
            choices = opts + [row["meaning"]]
            random.shuffle(choices)
            self.buttons = []
            for c in choices:
                b = tk.Button(self.area, text=c, wraplength=460, bg=BTN, fg=FG, relief="flat",
                              pady=6, font=("Segoe UI", 10),
                              command=lambda c=c: self._answer(c == row["meaning"], c))
                b.pack(fill="x", pady=3)
                self.buttons.append((b, c))
        else:                                               # gõ nghĩa
            self.mode = "type"
            self.prompt.config(text="Gõ nghĩa của từ:")
            self.var = tk.StringVar()
            e = tk.Entry(self.area, textvariable=self.var, font=("Segoe UI", 15), justify="center")
            e.pack(fill="x", pady=6)
            e.focus_set()
            self.entry = e

            def submit(_=None):
                self._answer(is_correct(self.var.get(), row["meaning"]), self.var.get())
                return "break"

            e.bind("<Return>", submit)
            tk.Button(self.area, text="Trả lời", command=submit, bg=BTN, fg=FG, relief="flat",
                      padx=16, pady=6).pack(pady=4)

    def _answer(self, ok: bool, given: str):
        if self.answered:
            return
        self.answered = True
        row = self.rows[self.i]
        self.db.record_test(row["id"], ok)
        self.correct += ok
        if self.mode == "mc":
            for b, c in self.buttons:
                b.config(state="disabled")
                if c == row["meaning"]:
                    b.config(bg=GOOD, fg="#11111b")
                elif c == given:
                    b.config(bg=BAD, fg="#11111b")
        else:
            self.entry.config(state="disabled")
        self.fb.config(
            text="Đúng rồi!" if ok else f"Sai. Đáp án: {row['meaning']}",
            fg=GOOD if ok else BAD,
        )
        self.next_btn.config(state="normal")
        self.next_btn.focus_set()

    def _next(self):
        self.i += 1
        if self.i >= len(self.rows):
            self._finish()
        else:
            self._show()

    def _finish(self):
        for w in self.area.winfo_children():
            w.destroy()
        self.finished = True
        if not self.db.pending_test_words():
            self.db.set("test_done_day", date.today().isoformat())
        total = len(self.rows)
        self.head.config(text="Hoàn thành bài test!")
        self.word_lbl.config(text=f"{self.correct}/{total}")
        self.prompt.config(text="Từ trả lời đúng được đánh dấu đã thuộc và sẽ không nhắc lại.")
        self.fb.config(text="", fg=FG)
        self.next_btn.config(text="Đóng", state="normal", command=self._close)

    def _on_close(self):
        if self.finished or messagebox.askyesno(
            "Chưa xong", "Bài test chưa hoàn thành. Thoát và làm tiếp sau?", parent=self
        ):
            self._close()

    def _close(self):
        if self._closed:
            return
        self._closed = True
        self.app.test_closed()
        self.destroy()


# ────────────────────────────── Cửa sổ chính + bộ đếm giờ ──────────────────────────────
class App:
    def __init__(self, root: tk.Tk, db: Database, background: bool, lock):
        self.root, self.db = root, db
        self.popup = self.test = None
        self.last_popup = datetime.now()          # popup đầu tiên sau 1 khoảng nhắc
        self.last_test_try = datetime.min
        self._show_requested = False
        self._hint_shown = False

        root.title("Phần mềm học Tiếng Anh")
        root.geometry("560x640")
        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.resizable(False,False)
        self._build_ui()
        self.db.prepare_today()
        self.refresh_stats()

        if background:
            root.withdraw()
        if lock is not None:
            threading.Thread(target=self._serve, args=(lock,), daemon=True).start()
        root.after(500, self._poll_show)
        root.after(3000, self._tick)

    # ---- giao diện ----
    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}
        f1 = ttk.LabelFrame(self.root, text="Thêm từ mới (mỗi dòng:  Từ_mới - Nghĩa)")
        f1.pack(fill="x", **pad)
        self.text = tk.Text(f1, height=9, font=("Consolas", 10), wrap="word")
        self.text.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(f1, text="Thêm vào danh sách", command=self.add_words).pack(pady=(0, 8))

        f2 = ttk.LabelFrame(self.root, text="Cài đặt")
        f2.pack(fill="x", **pad)
        self.v_interval = tk.IntVar(value=self.db.get_int("interval_min", 60))
        self.v_words = tk.IntVar(value=self.db.get_int("words_per_day", 10))
        self.v_test = tk.StringVar(value=self.db.get("test_time"))
        ttk.Label(f2, text="Nhắc từ mới mỗi (phút):").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Spinbox(f2, from_=1, to=720, textvariable=self.v_interval, width=6).grid(row=0, column=1, sticky="w")
        ttk.Label(f2, text="Số từ học mỗi ngày:").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Spinbox(f2, from_=1, to=100, textvariable=self.v_words, width=6).grid(row=1, column=1, sticky="w")
        ttk.Label(f2, text="Giờ làm bài test (HH:MM):").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(f2, textvariable=self.v_test, width=8).grid(row=2, column=1, sticky="w")
        self.v_startup = tk.BooleanVar(value=is_startup_enabled())
        cb = ttk.Checkbutton(f2, text="Tự chạy ngầm khi bật máy (Windows)",
                             variable=self.v_startup, command=self.toggle_startup)
        cb.grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        if not startup_supported():
            cb.state(["disabled"])
        ttk.Button(f2, text="Lưu cài đặt", command=self.save_settings).grid(row=0, column=2, rowspan=2, padx=20)

        f3 = ttk.LabelFrame(self.root, text="Hôm nay")
        f3.pack(fill="x", **pad)
        self.stats_lbl = ttk.Label(f3, justify="left")
        self.stats_lbl.pack(anchor="w", padx=8, pady=6)
        bar = ttk.Frame(f3)
        bar.pack(pady=(0, 8))
        ttk.Button(bar, text="Học ngay", command=lambda: self.open_popup(force=True)).pack(side="left", padx=4)
        ttk.Button(bar, text="Làm bài test ngay", command=lambda: self.open_test(force=True)).pack(side="left", padx=4)

        f4 = ttk.Frame(self.root)
        f4.pack(fill="x", **pad)
        ttk.Button(f4, text="Thu nhỏ chạy ngầm", command=self.hide).pack(side="left")
        ttk.Button(f4, text="Thoát hẳn", command=self.quit).pack(side="right")

    def refresh_stats(self):
        s = self.db.stats()
        self.stats_lbl.config(text=(
            f"Từ của bạn: {s['user_new']} chưa thuộc, {s['user_done']} đã thuộc  |  "
            f"Thư viện còn {s['lib_left']} từ\n"
            f"Danh sách hôm nay: {s['today']} từ  |  đã nhắc: {s['spammed']}  |  test đúng: {s['correct']}"
        ))

    def add_words(self):
        raw = self.text.get("1.0", "end")
        added, updated, bad = self.db.add_user_words(raw)
        if added or updated:
            self.text.delete("1.0", "end")
            self.db.prepare_today()          # bổ sung ngay vào danh sách hôm nay nếu còn chỗ
        self.refresh_stats()
        msg = f"Đã thêm {added} từ mới, cập nhật {updated} từ."
        if bad:
            msg += f"\n\n{len(bad)} dòng sai định dạng (cần 'Từ - Nghĩa'):\n" + "\n".join(bad[:3])
        messagebox.showinfo("Kết quả", msg)

    def save_settings(self):
        try:
            interval, words = int(self.v_interval.get()), int(self.v_words.get())
            h, m = self.v_test.get().strip().split(":")
            dtime(int(h), int(m))
            assert interval >= 1 and words >= 1
        except Exception:
            messagebox.showerror("Lỗi", "Kiểm tra lại: số phút/số từ phải là số dương, giờ test dạng HH:MM.")
            return
        self.db.set("interval_min", interval)
        self.db.set("words_per_day", words)
        self.db.set("test_time", f"{int(h):02d}:{int(m):02d}")
        self.db.prepare_today()
        self.refresh_stats()
        messagebox.showinfo("OK", "Đã lưu cài đặt.")

    def toggle_startup(self):
        try:
            set_startup(self.v_startup.get())
        except Exception as e:
            self.v_startup.set(not self.v_startup.get())
            messagebox.showerror("Lỗi", f"Không đổi được chế độ khởi động: {e}")

    # ---- ẩn / hiện / thoát ----
    def hide(self):
        if not self._hint_shown:
            self._hint_shown = True
            messagebox.showinfo(
                "Chạy ngầm",
                "Chương trình vẫn chạy ngầm để nhắc từ.\n"
                "Mở lại file .exe để hiện cửa sổ này; bấm 'Thoát hẳn' để tắt hoàn toàn.",
            )
        self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.refresh_stats()

    def quit(self):
        self.root.destroy()

    def _serve(self, lock):
        while True:
            try:
                conn, _ = lock.accept()
                conn.recv(16)
                conn.close()
                self._show_requested = True
            except OSError:
                break

    def _poll_show(self):
        if self._show_requested:
            self._show_requested = False
            self.show()
        self.root.after(500, self._poll_show)

    # ---- bộ đếm giờ chạy ngầm ----
    def _busy(self):
        return self.popup is not None or self.test is not None

    def popup_closed(self):
        self.popup = None
        self.last_popup = datetime.now()
        self.refresh_stats()

    def test_closed(self):
        self.test = None
        self.refresh_stats()

    def _test_done_today(self):
        return self.db.get("test_done_day") == date.today().isoformat()

    def _tick(self):
        try:
            self.db.prepare_today()
            now = datetime.now()
            test_time = parse_hhmm(self.db.get("test_time"))
            if not self._busy() and not self._test_done_today():
                if now.time() >= test_time:
                    if now - self.last_test_try >= timedelta(minutes=TEST_RETRY_MIN):
                        self.open_test()
                else:
                    interval = timedelta(minutes=self.db.get_int("interval_min", 60))
                    if now - self.last_popup >= interval:
                        self.open_popup()
        except Exception as e:  # bộ đếm giờ không được chết vì 1 lỗi lặt vặt
            print("tick error:", e, file=sys.stderr)
        finally:
            self.root.after(TICK_MS, self._tick)

    def open_popup(self, force=False):
        if self._busy():
            return
        row = self.db.next_spam_word()
        if row is None:
            if force:
                messagebox.showinfo("Trống", "Hôm nay chưa có từ nào. Hãy thêm từ hoặc kiểm tra thư viện.")
            return
        self.db.bump_spam(row["id"])
        self.last_popup = datetime.now()
        self.popup = SpamPopup(self, row)

    def open_test(self, force=False):
        if self._busy():
            return
        self.last_test_try = datetime.now()
        rows = self.db.pending_test_words()
        if not rows:
            if not force:
                self.db.set("test_done_day", date.today().isoformat())
            else:
                messagebox.showinfo("Trống", "Hôm nay không còn từ nào cần test.")
            return
        self.test = TestWindow(self, rows)


def main():
    background = "--background" in sys.argv
    lock = acquire_lock(notify_existing=not background)
    if lock is None:
        return
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root, Database(), background, lock)
    root.mainloop()


if __name__ == "__main__":
    main()
