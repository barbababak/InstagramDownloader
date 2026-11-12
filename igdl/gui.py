"""The Windows interface (Persian, right-to-left)."""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QIcon, QTextOption
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from . import __version__, auth, config, naming
from .engine import MODE_AUTO, MODE_CAPTIONS, MODE_FULL, MODE_NEW, MODE_OLD, Plan, SyncEngine
from .models import FatalSourceError
from .source import InstagramSource
from .state import ProfileState, StateError

MODES = [
    (MODE_AUTO, "خودکار: اول جدیدها، بعد ادامهٔ قدیمی‌ها (پیشنهادی)"),
    (MODE_NEW, "فقط پست‌های جدید"),
    (MODE_OLD, "فقط ادامهٔ پست‌های قدیمی"),
    (MODE_FULL, "بررسی کامل همهٔ پست‌ها و فایل‌های پاک‌شده"),
    (MODE_CAPTIONS, "فقط کپشن همهٔ پست‌ها (بدون دانلود ویدیو و تصویر)"),
]


_LEFT_TO_RIGHT_RUN = re.compile(r"\d{4}-\d{2}-\d{2}|\d+(?:\.\d+)? (?:B|KB|MB|GB)(?:/s)?|\.[a-z0-9]{2,5}$", re.MULTILINE)


def bidi_safe(text: str) -> str:
    """Keeps dates, sizes and file extensions readable inside right-to-left text."""
    return _LEFT_TO_RIGHT_RUN.sub(lambda match: f"⁦{match.group()}⁩", text)


def format_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def describe_plan(plan: Plan) -> str:
    if plan.first_run:
        total = f"{plan.total} پست" if plan.total is not None else "تعداد پست‌ها نامشخص"
        return f"اولین اجرا برای این پروفایل ({total}). دانلود از جدیدترین پست شروع می‌شود."
    parts = []
    if plan.new_posts:
        parts.append(f"{plan.new_posts}{'+' if plan.new_posts_more else ''} پست جدید")
    else:
        parts.append("پست جدیدی نیست")
    if plan.backfill_complete:
        parts.append("همهٔ پست‌های قدیمی گرفته شده")
    elif plan.remaining_old is not None:
        parts.append(f"حدود {plan.remaining_old} پست قدیمی مانده")
    else:
        parts.append("پست‌های قدیمی هنوز کامل نشده")
    if plan.failed:
        parts.append(f"{plan.failed} پست ناموفق برای تلاش دوباره")
    return "، ".join(parts) + f". تا الان {plan.done} پست دانلود شده."


class Task(QThread):
    """Runs one function off the UI thread."""
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, work: Callable[[], object], parent: Optional[QObject] = None):
        super().__init__(parent)
        self._work = work

    def run(self) -> None:
        try:
            self.succeeded.emit(self._work())
        except Exception as error:  # shown to the user; the thread must not die silently
            self.failed.emit(str(error))


class SyncWorker(QThread):
    event = Signal(str, object)
    finished_with = Signal(str, str, bool)  # result, message, needs_login

    def __init__(self, username: str, settings: config.Settings, mode: Optional[str], limit: int,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.stop_event = threading.Event()
        self._username = username
        self._settings = settings
        self._mode = mode  # None: only analyze
        self._limit = limit

    def run(self) -> None:
        try:
            folder = Path(self._settings.download_root) / self._username
            folder.mkdir(parents=True, exist_ok=True)
            state = ProfileState(folder, self._username)
            source = InstagramSource(
                config.load_session(), self.stop_event,
                on_wait=lambda text: self.event.emit("status", text),
                on_message=lambda text: self.event.emit("log", text))
            engine = SyncEngine(source, state, self._settings, self.event.emit)
            if self._mode is None:
                engine.analyze()
                result = "analyzed"
            else:
                result = engine.run(self._mode, self._limit)
            self.finished_with.emit(result, "", False)
        except FatalSourceError as error:
            self.finished_with.emit("error", str(error), error.needs_login)
        except (StateError, OSError) as error:
            self.finished_with.emit("error", str(error), False)
        except Exception as error:  # an unexpected Instagram response must not take the window down
            self.finished_with.emit("error", f"{type(error).__name__}: {error}", False)


class LoginDialog(QDialog):
    def __init__(self, settings: config.Settings, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("ورود به اینستاگرام")
        self.setMinimumWidth(520)
        self._settings = settings
        self._task: Optional[Task] = None
        self.username: Optional[str] = None

        layout = QVBoxLayout(self)
        intro = QLabel("برنامه رمز عبور نمی‌خواهد و از نشست همان مرورگری استفاده می‌کند که در آن وارد شده‌اید. "
                       "کوکی‌ها فقط روی همین کامپیوتر می‌مانند. بهتر است برای دانلودهای زیاد از حساب اصلی‌تان "
                       "استفاده نکنید؛ اینستاگرام ممکن است حساب‌های پرمصرف را محدود کند.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        browser_box = QGroupBox("روش ۱: خواندن از مرورگر")
        browser_row = QHBoxLayout(browser_box)
        self.browser = QComboBox()
        for key, label in auth.BROWSERS.items():
            self.browser.addItem(label, key)
        self.browser.setCurrentIndex(max(self.browser.findData(settings.login_browser), 0))
        self.from_browser = QPushButton("خواندن از مرورگر")
        self.from_browser.clicked.connect(self._use_browser)
        browser_row.addWidget(self.browser, 1)
        browser_row.addWidget(self.from_browser)
        layout.addWidget(browser_box)

        paste_box = QGroupBox("روش ۲: چسباندن کوکی")
        paste_layout = QVBoxLayout(paste_box)
        hint = QLabel("در مرورگر: F12 ← Application/Storage ← Cookies ← instagram.com، "
                      "مقدار sessionid را کپی کنید و اینجا بچسبانید.")
        hint.setWordWrap(True)
        self.cookie_text = QLineEdit()
        self.cookie_text.setEchoMode(QLineEdit.EchoMode.Password)
        self.cookie_text.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.cookie_text.setPlaceholderText("sessionid")
        self.from_text = QPushButton("ورود با این کوکی")
        self.from_text.clicked.connect(self._use_text)
        paste_layout.addWidget(hint)
        paste_layout.addWidget(self.cookie_text)
        paste_layout.addWidget(self.from_text)
        layout.addWidget(paste_box)

        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _use_browser(self) -> None:
        browser = self.browser.currentData()
        self._settings.login_browser = browser
        self._settings.save()
        self._verify(lambda: auth.cookies_from_browser(browser))

    def _use_text(self) -> None:
        text = self.cookie_text.text()
        self._verify(lambda: auth.cookies_from_text(text))

    def _verify(self, get_cookies: Callable[[], dict]) -> None:
        def work() -> str:
            cookies = get_cookies()
            username = InstagramSource({"username": "", "cookies": cookies}).verify_login()
            if not username:
                raise auth.AuthError("اینستاگرام این نشست را نپذیرفت؛ دوباره وارد حساب شوید و امتحان کنید.")
            config.save_session(username, cookies)
            return username

        self.from_browser.setEnabled(False)
        self.from_text.setEnabled(False)
        self.message.setText("در حال بررسی نشست...")
        self._task = Task(work, self)
        self._task.succeeded.connect(self._verified)
        self._task.failed.connect(self._verify_failed)
        self._task.start()

    def _verified(self, username: str) -> None:
        self.username = username
        self.accept()

    def _verify_failed(self, text: str) -> None:
        self.message.setText(text)
        self.from_browser.setEnabled(True)
        self.from_text.setEnabled(True)

    def reject(self) -> None:
        if self._task is None or not self._task.isRunning():
            super().reject()


class StatCard(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(0)
        caption = QLabel(title)
        caption.setObjectName("cardTitle")
        self.value = QLabel("–")
        self.value.setObjectName("cardValue")
        self.detail = QLabel("")
        self.detail.setObjectName("cardTitle")
        layout.addWidget(caption)
        layout.addWidget(self.value)
        layout.addWidget(self.detail)

    def set(self, value: str, detail: str = "") -> None:
        self.value.setText(bidi_safe(value))
        self.detail.setText(bidi_safe(detail))


class MainWindow(QMainWindow):
    def __init__(self, settings: config.Settings):
        super().__init__()
        self.settings = settings
        self.worker: Optional[SyncWorker] = None
        self._speed_mark = (time.monotonic(), 0)
        self._file_bytes = 0
        self._speed = 0.0
        self._close_after_stop = False
        self.setWindowTitle(f"دانلودر اینستاگرام {__version__}")
        self.resize(980, 720)
        self._build()
        self._refresh_login()
        self._profile_changed()

    # ---- layout ----

    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setSpacing(10)

        form = QGridLayout()
        form.setColumnStretch(1, 1)
        form.addWidget(QLabel("پروفایل:"), 0, 0)
        self.profile = QComboBox()
        self.profile.setEditable(True)
        self.profile.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.profile.addItems(self.settings.profiles)
        self.profile.setCurrentText(self.settings.last_profile)
        self.profile.lineEdit().setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.profile.lineEdit().setPlaceholderText("نام کاربری یا نشانی پروفایل")
        self.profile.currentTextChanged.connect(self._profile_changed)
        form.addWidget(self.profile, 0, 1)
        self.login_label = QLabel("")
        form.addWidget(self.login_label, 0, 2)
        self.login_button = QPushButton("ورود")
        self.login_button.clicked.connect(self._login_clicked)
        form.addWidget(self.login_button, 0, 3)

        form.addWidget(QLabel("پوشهٔ دانلود:"), 1, 0)
        self.root_edit = QLineEdit(self.settings.download_root)
        self.root_edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.root_edit.editingFinished.connect(self._root_edited)
        form.addWidget(self.root_edit, 1, 1)
        browse = QPushButton("انتخاب...")
        browse.clicked.connect(self._browse)
        form.addWidget(browse, 1, 2)
        open_folder = QPushButton("بازکردن پوشه")
        open_folder.clicked.connect(self._open_folder)
        form.addWidget(open_folder, 1, 3)

        form.addWidget(QLabel("روش دانلود:"), 2, 0)
        self.mode = QComboBox()
        for key, label in MODES:
            self.mode.addItem(label, key)
        self.mode.setCurrentIndex(max(self.mode.findData(self.settings.sync_mode), 0))
        self.mode.currentIndexChanged.connect(self._options_changed)
        form.addWidget(self.mode, 2, 1)
        form.addWidget(QLabel("سقف پست در هر اجرا:"), 2, 2)
        self.limit = QSpinBox()
        self.limit.setRange(0, 100000)
        self.limit.setSpecialValueText("بدون سقف")
        self.limit.setMinimumWidth(120)
        self.limit.setValue(self.settings.max_posts_per_run)
        self.limit.valueChanged.connect(self._options_changed)
        form.addWidget(self.limit, 2, 3)
        layout.addLayout(form)

        actions = QHBoxLayout()
        self.start_button = QPushButton("شروع / ادامهٔ دانلود")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(lambda: self._start(self.mode.currentData()))
        self.check_button = QPushButton("بررسی وضعیت")
        self.check_button.clicked.connect(lambda: self._start(None))
        self.stop_button = QPushButton("توقف")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        actions.addWidget(self.start_button, 2)
        actions.addWidget(self.check_button, 1)
        actions.addWidget(self.stop_button, 1)
        layout.addLayout(actions)

        self.plan_label = QLabel("")
        self.plan_label.setObjectName("plan")
        self.plan_label.setWordWrap(True)
        layout.addWidget(self.plan_label)

        cards = QHBoxLayout()
        self.card_posts = StatCard("پست‌ها")
        self.card_videos = StatCard("ویدیوها")
        self.card_images = StatCard("تصاویر")
        self.card_size = StatCard("حجم کل")
        self.card_failed = StatCard("ناموفق")
        for card in (self.card_posts, self.card_videos, self.card_images, self.card_size, self.card_failed):
            cards.addWidget(card)
        layout.addLayout(cards)

        self.overall = QProgressBar()
        self.overall.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overall.setFormat("%v از %m پست (%p%)")
        layout.addWidget(self.overall)
        self.status = QLabel("آماده")
        self.status.setMinimumHeight(self.status.fontMetrics().lineSpacing() * 2)
        layout.addWidget(self.status)
        self.file_bar = QProgressBar()
        self.file_bar.setFixedHeight(10)
        self.file_bar.setTextVisible(False)
        layout.addWidget(self.file_bar)

        self.tabs = QTabWidget()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        # A line starts with a time, so without this each line picks its own direction.
        self.log.document().setDefaultTextOption(QTextOption(Qt.AlignmentFlag.AlignRight))
        self.tabs.addTab(self.log, "گزارش")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["تاریخ", "عنوان / فایل", "محتوا", "حجم"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setToolTip("با دوبار کلیک، صفحهٔ پست در مرورگر باز می‌شود")
        self.table.cellDoubleClicked.connect(self._open_post)
        self.tabs.addTab(self.table, "پست‌های دانلودشده")
        layout.addWidget(self.tabs, 1)

    # ---- helpers ----

    def _username(self) -> Optional[str]:
        return naming.parse_username(self.profile.currentText())

    def _folder(self) -> Optional[Path]:
        username = self._username()
        return Path(self.settings.download_root) / username if username else None

    def _read_state(self) -> Optional[ProfileState]:
        folder, username = self._folder(), self._username()
        if not folder or not (folder / "_state.json").exists():
            return None
        try:
            return ProfileState(folder, username)
        except StateError as error:
            self._append_log(str(error))
            return None

    def _set_status(self, text: str) -> None:
        self.status.setText(bidi_safe(text))

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(f"{datetime.now():%H:%M:%S}  {bidi_safe(text)}")

    def _refresh_login(self) -> None:
        session = config.load_session()
        self.login_label.setText(f"حساب: {session['username']}" if session else "بدون ورود")
        self.login_button.setText("خروج" if session else "ورود")

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.check_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.login_button.setEnabled(not running)
        self.profile.setEnabled(not running)
        self.root_edit.setEnabled(not running)

    def _show_totals(self, totals: dict, session: Optional[dict] = None) -> None:
        session = session or {}
        total = totals.get("total_posts")
        posts = totals["posts"]
        self.card_posts.set(f"{posts} از {total}" if total is not None else str(posts),
                            f"این اجرا: {session['posts']}" if session else "")
        self.card_videos.set(str(totals["videos"]), f"این اجرا: {session['videos']}" if session else "")
        self.card_images.set(str(totals["images"]), f"این اجرا: {session['images']}" if session else "")
        self.card_size.set(format_size(totals["bytes"]),
                           f"این اجرا: {format_size(session['bytes'])}" if session else "")
        self.card_failed.set(str(totals["failed"]))
        if total:
            self.overall.setRange(0, max(total, posts))
            self.overall.setFormat("%v از %m پست (%p%)")
            self.overall.setValue(posts)
        else:
            # Without a login Instagram does not report the post count.
            self.overall.setRange(0, 1)
            self.overall.setValue(0)
            self.overall.setFormat(f"{posts} پست (تعداد کل را اینستاگرام بدون ورود اعلام نمی‌کند)")

    def _fill_table(self, posts: dict) -> None:
        rows = sorted(posts.items(), key=lambda pair: pair[1]["timestamp"], reverse=True)
        self.table.setRowCount(len(rows))
        for row, (shortcode, post) in enumerate(rows):
            videos = sum(item["is_video"] for item in post["files"])
            images = len(post["files"]) - videos
            content = "، ".join(part for part in (f"{videos} ویدیو" if videos else "",
                                                  f"{images} تصویر" if images else "") if part)
            values = [naming.format_date(post["timestamp"]), post["base"].split(" - ", 1)[-1], content,
                      format_size(sum(item["size"] for item in post["files"]))]
            for column, value in enumerate(values):
                item = QTableWidgetItem(bidi_safe(value))
                item.setData(Qt.ItemDataRole.UserRole, shortcode)
                self.table.setItem(row, column, item)
        self.table.resizeColumnToContents(0)

    # ---- events ----

    def _profile_changed(self) -> None:
        if self.worker:
            return
        state = self._read_state()
        self._show_totals(state.totals() if state else
                          {"posts": 0, "total_posts": None, "images": 0, "videos": 0, "bytes": 0, "failed": 0})
        self._fill_table(state.posts if state else {})
        self.plan_label.setText("")

    def _options_changed(self) -> None:
        self.settings.sync_mode = self.mode.currentData()
        self.settings.max_posts_per_run = self.limit.value()
        self.settings.save()

    def _root_edited(self) -> None:
        text = self.root_edit.text().strip()
        if text and text != self.settings.download_root:
            self.settings.download_root = text
            self.settings.save()
            self._profile_changed()

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "پوشهٔ دانلود", self.settings.download_root)
        if folder:
            self.root_edit.setText(os.path.normpath(folder))
            self._root_edited()

    def _open_folder(self) -> None:
        folder = self._folder() if self._folder() and self._folder().exists() else Path(self.settings.download_root)
        if folder.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        else:
            self._set_status("این پوشه هنوز ساخته نشده است.")

    def _open_post(self, row: int, _column: int) -> None:
        shortcode = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        QDesktopServices.openUrl(QUrl(f"https://www.instagram.com/p/{shortcode}/"))

    def _login_clicked(self) -> None:
        if config.load_session():
            config.clear_session()
            self._append_log("از حساب خارج شدید و کوکی‌های ذخیره‌شده پاک شد.")
        else:
            dialog = LoginDialog(self.settings, self)
            if dialog.exec() and dialog.username:
                self._append_log(f"با حساب {dialog.username} وارد شدید.")
        self._refresh_login()

    def _start(self, mode: Optional[str]) -> None:
        username = self._username()
        if not username:
            QMessageBox.warning(self, "پروفایل", "نام کاربری یا نشانی پروفایل معتبر نیست.")
            return
        self._root_edited()
        self.profile.setCurrentText(username)
        if username not in self.settings.profiles:
            self.settings.profiles.append(username)
            self.profile.addItem(username)
        self.settings.last_profile = username
        self.settings.save()

        self._set_running(True)
        self._speed_mark = (time.monotonic(), 0)
        self._speed = 0.0
        self.file_bar.setRange(0, 0)
        self._set_status("در حال اتصال...")
        self.worker = SyncWorker(username, self.settings, mode, self.limit.value(), self)
        self.worker.event.connect(self._on_event)
        self.worker.finished_with.connect(self._on_finished)
        self.worker.start()

    def _stop(self) -> None:
        if self.worker:
            self.worker.stop_event.set()
            self.stop_button.setEnabled(False)
            self._set_status("در حال توقف...")

    def _on_event(self, kind: str, data: object) -> None:
        if kind == "log":
            self._append_log(str(data))
        elif kind == "status":
            if not self.worker.stop_event.is_set():
                self._set_status(str(data))
        elif kind == "plan":
            self.plan_label.setText(describe_plan(data))
            self._append_log(describe_plan(data))
        elif kind == "stats":
            self._show_totals(data, data["session"])
        elif kind == "posts":
            self._fill_table(data)
        elif kind == "file_progress":
            self._on_file_progress(*data)

    def _on_file_progress(self, name: str, done: int, total: int) -> None:
        if total:
            self.file_bar.setRange(0, 1000)
            self.file_bar.setValue(int(done * 1000 / total))
        now = time.monotonic()
        mark_time, mark_done = self._speed_mark
        if done < mark_done:
            mark_time, mark_done = now, 0
        if now - mark_time >= 0.5:
            self._speed = (done - mark_done) / (now - mark_time)
            mark_time, mark_done = now, done
        self._speed_mark = (mark_time, mark_done)
        if not self.worker.stop_event.is_set():
            of_total = f" از {format_size(total)}" if total else ""
            self._set_status(f"دانلود: {name}\n{format_size(done)}{of_total}  ·  {format_size(self._speed)}/s")

    def _on_finished(self, result: str, message: str, needs_login: bool) -> None:
        self.worker.wait()
        self.worker = None
        self._set_running(False)
        self.file_bar.setRange(0, 1)
        self.file_bar.setValue(0)
        final = {
            "done": "کار تمام شد؛ همه‌چیز به‌روز است.",
            "stopped": "متوقف شد. پیشرفت ذخیره شده است.",
            "limit": "به سقف این اجرا رسید. پیشرفت ذخیره شده است.",
            "analyzed": "بررسی تمام شد.",
        }.get(result, "با خطا متوقف شد. پیشرفت ذخیره شده است.")
        self._set_status(final)
        self._append_log(final)
        if self._close_after_stop:
            self.close()
            return
        if result == "error":
            self._append_log(message)
            if needs_login and not config.load_session():
                answer = QMessageBox.question(self, "نیاز به ورود",
                                              f"{message}\n\nالان وارد حساب می‌شوید؟")
                if answer == QMessageBox.StandardButton.Yes:
                    self._login_clicked()
            else:
                QMessageBox.warning(self, "خطا", message)

    def closeEvent(self, event) -> None:
        if self.worker:
            self._close_after_stop = True
            self._stop()
            event.ignore()
        else:
            event.accept()


STYLE = """
QLabel#plan { font-weight: 600; padding: 2px 0; }
QFrame#card { border: 1px solid palette(mid); border-radius: 6px; background: palette(base); }
QLabel#cardTitle { color: palette(placeholder-text); font-size: 9pt; }
QLabel#cardValue { font-size: 15pt; font-weight: 600; }
QPushButton#primary { font-weight: 600; padding: 6px; }
"""


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(str(config.ICON_PATH)))
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(STYLE)
    window = MainWindow(config.Settings.load())
    window.show()
    return app.exec()
