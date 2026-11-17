"""BTC · ETH 바이낸스 선물 데모 자동매매 · 안드로이드 앱 (Kivy).

전략·주문·백테스트는 core.py(원본 로직)를 그대로 사용하고,
이 파일은 모바일 화면과 스레드 연결만 담당합니다. 바이낸스 '데모' 서버 전용입니다.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from queue import Empty, Full, Queue, SimpleQueue
from threading import Event, Thread

from kivy.app import App
from kivy.clock import Clock
from kivy.core.text import LabelBase
from kivy.core.window import Window
from kivy.graphics import Color, Line, Rectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget

import core
from core import (
    DEFAULT_ENTRY, DEFAULT_SHORT_ENTRY, EntrySettings, MAX_WAIT_MINUTES,
    PROGRAM_CREDITS, SYMBOL, SYMBOLS, TIMEFRAMES, DEFAULT_TIMEFRAME,
    authenticate_demo, parse_order_settings, ranked_backtests, run_backtest,
    run_demo, run_symbols, validate_key,
)

HERE = Path(__file__).resolve().parent
FONT = str(HERE / "fonts" / "NanumGothic-Regular.ttf")
FONT_BOLD = str(HERE / "fonts" / "NanumGothic-Bold.ttf")
# Kivy 기본 글꼴(Roboto)에는 한글이 없어서 같은 이름으로 한글 글꼴을 등록합니다.
LabelBase.register(name="Roboto", fn_regular=FONT, fn_bold=FONT_BOLD)

BACKGROUND = (0.929, 0.949, 0.969, 1)
SURFACE = (1, 1, 1, 1)
INK = (0.09, 0.17, 0.25, 1)
MUTED = (0.30, 0.38, 0.47, 1)
NAVY = (0.078, 0.173, 0.282, 1)
TEAL = (0.0, 0.427, 0.384, 1)
PLUM = (0.541, 0.216, 0.337, 1)
BLUE = (0.0, 0.353, 0.612, 1)
SOFT = (0.9, 0.93, 0.97, 1)

DIRECTIONS = {"롱 전용": "long", "숏 전용": "short", "양방향": "both"}
MAX_LOG_LINES = 400
KEY_FILE = "demo_keys.json"


def on_android():
    return "ANDROID_ARGUMENT" in os.environ or hasattr(sys, "getandroidapilevel")


def keep_screen_on(enabled):
    """자동매매 중에는 화면이 꺼지지 않게 합니다 (안드로이드 전용)."""
    if not on_android():
        return
    try:
        from android.runnable import run_on_ui_thread
        from jnius import autoclass

        @run_on_ui_thread
        def apply():
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            flag = autoclass("android.view.WindowManager$LayoutParams").FLAG_KEEP_SCREEN_ON
            window = activity.getWindow()
            if enabled:
                window.addFlags(flag)
            else:
                window.clearFlags(flag)

        apply()
    except Exception:
        pass


def request_permissions():
    if not on_android():
        return
    try:
        from android.permissions import Permission, request_permissions as ask
        ask([Permission.INTERNET])
    except Exception:
        pass


# ───────────────────────── 위젯 도우미 ─────────────────────────

def card(accent=None):
    box = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(10), spacing=dp(6))
    box.bind(minimum_height=box.setter("height"))
    with box.canvas.before:
        Color(*SURFACE)
        rect = Rectangle(pos=box.pos, size=box.size)
        Color(*(accent or SURFACE))
        bar = Rectangle(pos=(box.x, box.top - dp(4)), size=(box.width, dp(4)))

    def update(*_):
        rect.pos, rect.size = box.pos, box.size
        bar.pos, bar.size = (box.x, box.top - dp(4)), (box.width, dp(4))

    box.bind(pos=update, size=update)
    return box


def make_label(text, *, color=INK, size=14, bold=False, height=None):
    widget = Label(text=text, color=color, font_size=sp(size), bold=bold, halign="left",
                   valign="middle", size_hint_y=None)
    widget.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
    if height:
        widget.height = dp(height)
    else:
        widget.bind(texture_size=lambda w, v: setattr(w, "height", max(dp(24), v[1] + dp(4))))
    return widget


def field(text="", *, password=False, hint="", numeric=False):
    return TextInput(
        text=str(text), password=password, hint_text=hint, multiline=False,
        input_type="number" if numeric else "text", write_tab=False,
        size_hint_y=None, height=dp(44), font_size=sp(15), padding=(dp(8), dp(11)),
        background_color=(0.97, 0.98, 0.99, 1), foreground_color=INK, cursor_color=INK,
    )


def make_button(text, color=BLUE, *, text_color=(1, 1, 1, 1), height=46, on_press=None):
    widget = Button(text=text, background_normal="", background_down="", background_color=color,
                    color=text_color, size_hint_y=None, height=dp(height), font_size=sp(15), bold=True)
    if on_press:
        widget.bind(on_release=lambda *_: on_press())
    return widget


def labeled_grid(pairs):
    """(이름, 위젯) 목록을 2열로 배치합니다."""
    grid = GridLayout(cols=2, size_hint_y=None, spacing=dp(6), row_default_height=dp(66),
                      row_force_default=True)
    grid.bind(minimum_height=grid.setter("height"))
    for name, widget in pairs:
        cell = BoxLayout(orientation="vertical", spacing=dp(2))
        cell.add_widget(make_label(name, color=MUTED, size=12, height=18))
        cell.add_widget(widget)
        grid.add_widget(cell)
    if len(pairs) % 2:
        grid.add_widget(Widget())
    return grid


class LogStream:
    """core.py의 print 출력을 화면 로그로 보냅니다."""

    def __init__(self, messages):
        self.messages = messages

    def write(self, text):
        if text:
            self.messages.put(("log", text))
            if text.startswith("실행 모드 · 데모 자동매매"):
                self.messages.put(("auto_ready", None))
        return len(text)

    def flush(self):
        pass


class DailyChart(Widget):
    """일자별 이익·손실 막대 차트."""

    def __init__(self, rows, **kwargs):
        super().__init__(**kwargs)
        self.rows = rows
        self.bind(pos=self.redraw, size=self.redraw)

    def redraw(self, *_):
        self.canvas.clear()
        for child in list(self.children):
            self.remove_widget(child)
        rows = self.rows
        if not rows or self.width < 10:
            return
        left, right = self.x + dp(48), self.right - dp(8)
        top, bottom = self.top - dp(8), self.y + dp(22)
        extent = max(1.0, *(abs(float(v)) for row in rows for v in row[1:])) * 1.1
        zero = (top + bottom) / 2
        scale = (top - bottom) / (2 * extent)
        slot = (right - left) / len(rows)
        with self.canvas:
            Color(0.78, 0.83, 0.88, 1)
            for fraction in (-1, -.5, 0, .5, 1):
                y = zero + fraction * extent * scale
                Line(points=[left, y, right, y], width=1)
            for index, (_date, gain, lost) in enumerate(rows):
                x = left + (index + .5) * slot
                for value, color, offset in ((gain, TEAL, -.16), (lost, PLUM, .16)):
                    if not value:
                        continue
                    Color(*color)
                    height = float(value) * scale
                    Rectangle(pos=(x + offset * slot - slot * .13, zero + min(0, height)),
                              size=(slot * .26, abs(height)))
        for fraction in (-1, 0, 1):
            self.add_widget(Label(text=f"{fraction * extent:,.0f}", color=MUTED, font_size=sp(10),
                                  size_hint=(None, None), size=(dp(44), dp(14)),
                                  pos=(self.x, zero + fraction * extent * scale - dp(7))))
        step = max(1, (len(rows) + 5) // 6)
        for index, (date, _, _) in enumerate(rows):
            if index % step == 0 or index == len(rows) - 1:
                self.add_widget(Label(text=date[5:], color=MUTED, font_size=sp(10), size_hint=(None, None),
                                      size=(dp(44), dp(14)),
                                      pos=(left + (index + .5) * slot - dp(22), self.y + dp(4))))


# ───────────────────────── 메인 앱 ─────────────────────────

class RsiApp(App):
    title = "BTC·ETH 데모 자동매매"

    def build(self):
        Window.clearcolor = BACKGROUND
        request_permissions()
        self.data_dir = core.configure_data_dir(Path(self.user_data_dir))
        self.key_path = self.data_dir / KEY_FILE

        self.messages = SimpleQueue()
        self.updates = SimpleQueue()
        self.manual_orders = Queue(maxsize=1)
        self.wake = Event()
        self.stop = Event()
        self.worker = None
        self.auto_running = False
        self.running_symbols = ()
        self.applied = DEFAULT_ENTRY
        self.log_text = ""

        root = BoxLayout(orientation="vertical")
        banner = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(64), padding=(dp(14), dp(6)))
        with banner.canvas.before:
            Color(*NAVY)
            self._banner_rect = Rectangle(pos=banner.pos, size=banner.size)
        banner.bind(pos=lambda w, v: setattr(self._banner_rect, "pos", v),
                    size=lambda w, v: setattr(self._banner_rect, "size", v))
        banner.add_widget(make_label("BTC · ETH 데모 자동매매", color=(1, 1, 1, 1), size=18, bold=True, height=28))
        banner.add_widget(make_label(PROGRAM_CREDITS, color=(0.83, 0.89, 0.95, 1), size=11, height=18))
        root.add_widget(banner)

        self.tabs = TabbedPanel(do_default_tab=False, tab_pos="bottom_mid", tab_height=dp(48),
                                background_color=BACKGROUND, background_image="")
        settings_tab = self._settings_tab()
        run_tab = self._run_tab()
        history_tab = self._history_tab()
        self.run_tab = run_tab
        for tab in (settings_tab, run_tab, history_tab):
            self.tabs.add_widget(tab)
        self.tabs.default_tab = settings_tab
        root.add_widget(self.tabs)

        self.load_saved_keys()
        Clock.schedule_interval(self.refresh, 0.2)
        Window.bind(on_keyboard=self._on_back)
        return root

    def _scroll(self):
        scroll = ScrollView(do_scroll_x=False, bar_width=dp(3))
        body = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(10), spacing=dp(10))
        body.bind(minimum_height=body.setter("height"))
        scroll.add_widget(body)
        return scroll, body

    def _settings_tab(self):
        tab = TabbedPanelItem(text="설정")
        scroll, body = self._scroll()

        general = card()
        self.direction = Spinner(text="양방향", values=tuple(DIRECTIONS), size_hint_y=None, height=dp(44),
                                 font_size=sp(15))
        self.timeframe = Spinner(text=DEFAULT_TIMEFRAME, values=tuple(TIMEFRAMES), size_hint_y=None,
                                 height=dp(44), font_size=sp(15))
        self.timeframe.bind(text=self.align_waits)
        general.add_widget(labeled_grid([("자동매매 방향", self.direction), ("RSI 기준 봉", self.timeframe)]))
        general.add_widget(make_label("양방향 동시 보유는 데모 계정의 헤지 모드가 필요합니다.", color=MUTED, size=12))
        self.symbol_boxes = {}
        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        row.add_widget(make_label("자동매매 종목", size=13, height=40))
        for symbol in SYMBOLS:
            box = CheckBox(active=True, size_hint_x=None, width=dp(36))
            self.symbol_boxes[symbol] = box
            row.add_widget(box)
            row.add_widget(make_label(symbol.replace("USDT", ""), size=13, height=40))
        general.add_widget(row)
        self.order_symbol = Spinner(text=SYMBOL, values=SYMBOLS, size_hint_y=None, height=dp(44), font_size=sp(15))
        general.add_widget(labeled_grid([("즉시 진입 · 백테스트 종목", self.order_symbol)]))
        general.add_widget(make_label("주문 비율은 종목별 사용 가능 잔액 기준입니다.", color=MUTED, size=12))
        body.add_widget(general)

        self.side_fields = {}
        for direction, title, accent in (("long", "롱 전용 설정", TEAL), ("short", "숏 전용 설정", PLUM)):
            defaults = DEFAULT_SHORT_ENTRY if direction == "short" else DEFAULT_ENTRY
            box = card(accent)
            box.add_widget(make_label(title, color=accent, size=17, bold=True, height=30))
            entries = self.side_fields[direction] = {
                "margin": field(defaults.order.margin_percent, numeric=True),
                "leverage": field(defaults.order.leverage, numeric=True),
                "stop": field(defaults.order.stop_percent, numeric=True),
                "take": field(defaults.order.take_percent, numeric=True),
                "first": field(f"{defaults.first_rsi:g}", numeric=True),
                "wait": field(defaults.wait_minutes, numeric=True),
                "second": field(f"{defaults.second_rsi:g}", numeric=True),
            }
            box.add_widget(make_label("주문 설정", size=14, bold=True, height=24))
            box.add_widget(labeled_grid([
                ("잔액 사용 (%)", entries["margin"]), ("격리 배율", entries["leverage"]),
                ("손절 (−%)", entries["stop"]), ("익절 (+%)", entries["take"]),
            ]))
            box.add_widget(make_label("손절·익절은 진입 증거금 대비 수익률입니다.", color=MUTED, size=12))
            box.add_widget(make_label("RSI(14) 진입 조건", size=14, bold=True, height=24))
            box.add_widget(labeled_grid([
                ("첫 RSI 기준", entries["first"]), ("최대 대기 (분)", entries["wait"]),
                ("두 번째 RSI 기준", entries["second"]),
            ]))
            hint = ("첫 기준 상향 돌파 마감 후, 다음 봉부터 진행 중 RSI가 두 번째 기준 미만이면 숏 진입합니다."
                    if direction == "short" else
                    "첫 기준 하향 돌파 마감 후, 다음 봉부터 진행 중 RSI가 두 번째 기준 이상이면 롱 진입합니다.")
            box.add_widget(make_label(hint, color=MUTED, size=12))
            box.add_widget(make_button(f"{'롱' if direction == 'long' else '숏'} 백테스트", accent,
                                       on_press=lambda d=direction: self.ask_backtest(d)))
            body.add_widget(box)

        keys = card()
        keys.add_widget(make_label("바이낸스 데모 API", size=15, bold=True, height=26))
        self.key_field = field(password=True, hint="API Key")
        self.secret_field = field(password=True, hint="Secret Key")
        keys.add_widget(self.key_field)
        keys.add_widget(self.secret_field)
        keys_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        keys_row.add_widget(make_button("키 저장", color=SOFT, text_color=INK, on_press=self.remember_keys))
        keys_row.add_widget(make_button("저장된 키 삭제", color=(0.98, 0.91, 0.94, 1), text_color=PLUM,
                                        on_press=self.forget_keys))
        keys.add_widget(keys_row)
        keys.add_widget(make_label("키 저장은 선택 사항이며 이 앱 전용 저장 공간에 저장됩니다. "
                                   "반드시 바이낸스 '데모' 키만 사용하세요.", color=MUTED, size=12))
        body.add_widget(keys)

        tab.add_widget(scroll)
        return tab

    def _run_tab(self):
        tab = TabbedPanelItem(text="실행")
        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        status_box = card()
        self.status = make_label("준비", size=14, bold=True)
        status_box.add_widget(self.status)
        root.add_widget(status_box)

        grid = GridLayout(cols=2, size_hint_y=None, spacing=dp(8), row_default_height=dp(46), row_force_default=True)
        grid.bind(minimum_height=grid.setter("height"))
        self.apply_button = make_button("설정 적용", color=SOFT, text_color=INK, on_press=self.apply)
        self.check_button = make_button("연결 확인", color=SOFT, text_color=INK,
                                        on_press=lambda: self.start(check_only=True))
        self.start_button = make_button("데모 시작", BLUE, on_press=lambda: self.start())
        self.stop_button = make_button("중지", (0.98, 0.91, 0.94, 1), text_color=(0.5, 0.16, 0.28, 1),
                                       on_press=self.request_stop)
        self.long_button = make_button("롱 시장가 즉시 진입", (0.88, 0.96, 0.94, 1), text_color=TEAL,
                                       on_press=lambda: self.start_manual("long"))
        self.short_button = make_button("숏 시장가 즉시 진입", (0.98, 0.91, 0.94, 1), text_color=PLUM,
                                        on_press=lambda: self.start_manual("short"))
        for widget in (self.apply_button, self.check_button, self.start_button, self.stop_button,
                       self.long_button, self.short_button):
            grid.add_widget(widget)
        root.add_widget(grid)
        self.set_idle_buttons()

        self.log_view = Label(text="", color=(0.93, 0.96, 0.99, 1), font_size=sp(12), halign="left",
                              valign="top", size_hint_y=None)
        self.log_view.bind(width=lambda w, v: setattr(w, "text_size", (v, None)),
                           texture_size=lambda w, v: setattr(w, "height", v[1] + dp(16)))
        self.log_scroll = ScrollView(do_scroll_x=False, bar_width=dp(3))
        with self.log_scroll.canvas.before:
            Color(*NAVY)
            self._log_rect = Rectangle(pos=self.log_scroll.pos, size=self.log_scroll.size)
        self.log_scroll.bind(pos=lambda w, v: setattr(self._log_rect, "pos", v),
                             size=lambda w, v: setattr(self._log_rect, "size", v))
        self.log_scroll.add_widget(self.log_view)
        root.add_widget(self.log_scroll)

        tab.add_widget(root)
        return tab

    def _history_tab(self):
        tab = TabbedPanelItem(text="기록")
        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))
        top = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.history_sort = Spinner(text="승률순", values=("승률순", "평가수익률순"), font_size=sp(15))
        self.history_sort.bind(text=lambda *_: self.refresh_history())
        top.add_widget(self.history_sort)
        top.add_widget(make_button("새로고침", color=SOFT, text_color=INK, on_press=self.refresh_history))
        root.add_widget(top)
        self.history_note = make_label("", color=MUTED, size=12)
        root.add_widget(self.history_note)
        scroll = ScrollView(do_scroll_x=False, bar_width=dp(3))
        self.history_list = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(8))
        self.history_list.bind(minimum_height=self.history_list.setter("height"))
        scroll.add_widget(self.history_list)
        root.add_widget(scroll)
        tab.add_widget(root)
        tab.bind(on_release=lambda *_: self.refresh_history())
        return tab

    # ── 키 저장 ──

    def load_saved_keys(self):
        try:
            values = json.loads(self.key_path.read_text(encoding="utf-8"))
            key, secret = validate_key(values[0], "API Key"), validate_key(values[1], "Secret Key")
        except FileNotFoundError:
            return
        except (OSError, ValueError, IndexError, TypeError):
            self.set_status("저장된 데모 키를 불러오지 못했습니다. 키를 다시 입력해 저장하세요.")
            return
        self.key_field.text, self.secret_field.text = key, secret
        self.set_status("저장된 데모 키를 불러왔습니다.")

    def remember_keys(self):
        try:
            keys = [validate_key(self.key_field.text, "API Key"), validate_key(self.secret_field.text, "Secret Key")]
        except ValueError as exc:
            self.set_status(str(exc))
            return
        try:
            self.key_path.write_text(json.dumps(keys), encoding="utf-8")
            os.chmod(self.key_path, 0o600)
        except OSError:
            self.set_status("데모 키를 저장하지 못했습니다.")
            return
        self.set_status("데모 키 저장 완료 · 다음 실행 시 자동으로 불러옵니다.")

    def forget_keys(self):
        try:
            self.key_path.unlink(missing_ok=True)
        except OSError:
            self.set_status("저장된 데모 키를 삭제하지 못했습니다.")
            return
        self.key_field.text = self.secret_field.text = ""
        self.set_status("저장된 데모 키와 입력란을 지웠습니다.")

    # ── 설정 읽기 (원본 GUI와 같은 검증) ──

    def align_waits(self, *_):
        minutes = TIMEFRAMES[self.timeframe.text][1]
        for entries in self.side_fields.values():
            value = entries["wait"].text
            if not value.isdecimal() or int(value) < minutes or int(value) % minutes:
                entries["wait"].text = str(minutes)

    def read_order(self, direction):
        entries = self.side_fields[direction]
        order = parse_order_settings(*(entries[name].text.strip() for name in ("margin", "leverage", "stop", "take")))
        if direction == "short" and order.take_percent >= order.leverage * 100:
            raise ValueError("숏 익절률은 레버리지 × 100%보다 작아야 합니다.")
        return order

    def read_side(self, direction):
        entries = self.side_fields[direction]
        try:
            first = float(entries["first"].text)
            second = float(entries["second"].text)
            wait = int(entries["wait"].text)
        except ValueError:
            raise ValueError("RSI는 숫자로, 대기 시간은 정수로 입력하세요.") from None
        if not (0 < first <= 100 and 0 < second <= 100):
            raise ValueError("RSI는 0 초과~100 이하로 입력하세요.")
        minutes = TIMEFRAMES[self.timeframe.text][1]
        if not minutes <= wait <= MAX_WAIT_MINUTES or wait % minutes:
            raise ValueError(f"대기 시간은 {minutes}~{MAX_WAIT_MINUTES}분 사이에서 {minutes}분 단위로 입력하세요.")
        return EntrySettings(first, wait, second, self.read_order(direction), direction,
                             timeframe=self.timeframe.text)

    def apply(self):
        direction = DIRECTIONS[self.direction.text]
        side = "long"
        try:
            long = self.read_side("long") if direction != "short" else DEFAULT_ENTRY
            side = "short"
            short = self.read_side("short") if direction != "long" else None
        except ValueError as exc:
            self.set_status(f"{'롱' if side == 'long' else '숏'} 설정 · {exc}")
            return False
        new_settings = EntrySettings(long.first_rsi, long.wait_minutes, long.second_rsi,
                                     long.order, direction, short, self.timeframe.text)
        if self.worker is not None and new_settings != self.applied:
            self.updates.put(new_settings)
            self.set_status("새 조건 적용 요청 · 적용 결과는 실행 로그에 표시됩니다.")
        else:
            self.set_status("진입 조건 설정 완료")
        self.applied = new_settings
        return True

    # ── 실행 제어 ──

    def set_status(self, text):
        self.status.text = text

    def set_idle_buttons(self):
        for widget in (self.apply_button, self.check_button, self.start_button, self.long_button, self.short_button):
            widget.disabled = False
        self.stop_button.disabled = True

    def set_busy_buttons(self):
        for widget in (self.check_button, self.start_button, self.long_button, self.short_button):
            widget.disabled = True
        self.stop_button.disabled = False

    def start(self, check_only=False, days=None, test_settings=None, manual_order=None):
        if self.worker is not None:
            return
        if days is None and not check_only and manual_order is None and not self.apply():
            return
        key = secret = None
        if days is None:
            try:
                key = validate_key(self.key_field.text, "API Key")
                secret = validate_key(self.secret_field.text, "Secret Key")
            except ValueError as exc:
                self.set_status(str(exc))
                return
        while not self.updates.empty():
            self.updates.get_nowait()
        while not self.manual_orders.empty():
            self.manual_orders.get_nowait()
        settings = test_settings if days is not None else self.applied
        selected = tuple(symbol for symbol, box in self.symbol_boxes.items() if box.active)
        target_symbol = self.order_symbol.text
        if days is None and not check_only and manual_order is None and not selected:
            self.set_status("자동매매 종목을 하나 이상 선택하세요.")
            return
        self.running_symbols = selected
        self.stop.clear()
        messages, stop = self.messages, self.stop
        updates, manual_orders, wake = self.updates, self.manual_orders, self.wake

        def run():
            stream = LogStream(messages)
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = stream
            result = "중지됨"
            try:
                if days is not None:
                    daily = run_backtest(settings, days, stop, target_symbol)
                    if not stop.is_set():
                        messages.put(("chart", (settings.direction, daily)))
                    result = "백테스트 완료 · 실행 탭의 로그를 확인하세요."
                else:
                    api, hedge_mode = authenticate_demo(key, secret)
                    api.symbol = target_symbol
                    if check_only:
                        result = "데모 연결 확인 완료 · " + ("헤지 모드" if hedge_mode else "단방향 모드")
                    elif not stop.is_set():
                        if manual_order is not None:
                            run_demo(api, hedge_mode, settings, stop=stop, manual_order=manual_order)
                        else:
                            run_symbols(api, hedge_mode, settings, selected, stop, updates, manual_orders, wake)
                        if manual_order is not None and not stop.is_set():
                            result = "시장가 진입 처리 종료 · 체결과 손절·익절은 로그를 확인하세요."
            except (RuntimeError, sqlite3.Error) as exc:
                print(f"중단 · {exc}", flush=True)
                result = "실행 중단 · 로그를 확인하세요."
            except Exception:
                print("중단 · 예상하지 못한 응답 또는 실행 오류가 발생했습니다.", flush=True)
                result = "실행 중단 · 로그를 확인하세요."
            finally:
                sys.stdout, sys.stderr = old_out, old_err
                messages.put(("done", result))

        self.set_busy_buttons()
        keep_screen_on(True)
        if manual_order is not None:
            self.set_status("데모 시장가 진입 처리 중")
        else:
            self.set_status("백테스트 실행 중" if days is not None
                            else "데모 연결 확인 중" if check_only else "데모 자동매매 시작 중")
        self.tabs.switch_to(self.run_tab)
        self.worker = Thread(target=run, daemon=True)
        self.worker.start()

    def start_manual(self, direction):
        if self.worker is not None and (not self.auto_running or self.stop.is_set()):
            return
        name = "롱" if direction == "long" else "숏"
        try:
            order = self.read_order(direction)
        except ValueError as exc:
            self.set_status(f"{name} 주문 설정 · {exc}")
            return
        request = (direction, order)
        if self.worker is None:
            self.start(manual_order=request)
            return
        if self.order_symbol.text not in self.running_symbols:
            self.set_status("선택한 종목을 자동매매 종목에 포함해 다시 시작하세요.")
            return
        try:
            self.manual_orders.put_nowait((self.order_symbol.text, *request, time.monotonic() + 15))
        except Full:
            self.set_status("앞선 시장가 진입 요청을 처리 중입니다.")
        else:
            self.wake.set()
            self.set_status(f"데모 {name} 시장가 진입 요청 접수 · 실행 로그를 확인하세요.")

    def ask_backtest(self, direction):
        name = "롱" if direction == "long" else "숏"
        try:
            settings = self.read_side(direction)
        except ValueError as exc:
            self.set_status(f"{name} 설정 · {exc}")
            self.tabs.switch_to(self.run_tab)
            return
        if self.worker is not None:
            self.set_status("실행 중에는 백테스트를 시작할 수 없습니다. 먼저 중지하세요.")
            self.tabs.switch_to(self.run_tab)
            return
        content = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        content.add_widget(make_label("최근 며칠을 테스트할까요? (1~30일)", size=14))
        days_field = field(7, numeric=True)
        content.add_widget(days_field)
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))

        def go():
            try:
                days = int(days_field.text)
                if not 1 <= days <= 30:
                    raise ValueError
            except ValueError:
                days_field.text = "7"
                return
            popup.dismiss()
            self.start(days=days, test_settings=settings)

        row.add_widget(make_button("취소", color=SOFT, text_color=INK, on_press=lambda: popup.dismiss()))
        row.add_widget(make_button("시작", BLUE, on_press=go))
        content.add_widget(row)
        popup = Popup(title=f"{name} 백테스트", content=content, size_hint=(0.9, None), height=dp(250))
        popup.open()

    def request_stop(self):
        self.stop.set()
        self.wake.set()
        for widget in (self.long_button, self.short_button):
            widget.disabled = True
        self.set_status("중지 요청 · 진행 중인 요청을 마무리하는 중")

    # ── 큐 처리 ──

    def append_log(self, text):
        self.log_text += text
        lines = self.log_text.splitlines(keepends=True)
        if len(lines) > MAX_LOG_LINES:
            lines = lines[-MAX_LOG_LINES:]
        self.log_text = "".join(lines)
        self.log_view.text = self.log_text
        Clock.schedule_once(lambda *_: setattr(self.log_scroll, "scroll_y", 0), 0.05)

    def refresh(self, _dt):
        try:
            while True:
                kind, message = self.messages.get_nowait()
                if kind == "done":
                    if self.worker is not None:
                        self.worker.join(timeout=2)
                    self.worker = None
                    self.auto_running = False
                    keep_screen_on(False)
                    self.set_idle_buttons()
                    self.set_status(message)
                elif kind == "auto_ready":
                    if not self.stop.is_set():
                        self.auto_running = True
                        for widget in (self.long_button, self.short_button):
                            widget.disabled = False
                        self.set_status("데모 자동매매 실행 중")
                elif kind == "chart":
                    self.show_daily_chart(*message)
                else:
                    self.append_log(message)
        except Empty:
            pass

    # ── 백테스트 결과·기록 ──

    def show_daily_chart(self, direction, rows):
        name = {"long": "롱", "short": "숏", "both": "양방향"}[direction]
        profit = sum(row[1] for row in rows)
        loss = sum(row[2] for row in rows)
        content = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(6))
        content.add_widget(make_label(f"이익 {profit:+,.2f}   손실 {loss:,.2f}   순손익 {profit + loss:+,.2f} USDT", size=13))
        content.add_widget(DailyChart(rows))
        content.add_widget(make_label("한국시간 · 청산 봉 시작일 기준 · 비용 제외 · 초록=이익, 자주=손실", color=MUTED, size=11))
        content.add_widget(make_button("닫기", on_press=lambda: popup.dismiss()))
        popup = Popup(title=f"{name} 백테스트 · 일자별 실현손익 (USDT)", content=content, size_hint=(0.96, 0.8))
        popup.open()

    def refresh_history(self, *_):
        self.history_list.clear_widgets()
        try:
            rows = ranked_backtests(self.history_sort.text)
        except (OSError, sqlite3.Error) as exc:
            self.history_note.text = f"기록을 읽을 수 없습니다. {exc}"
            return
        self.history_note.text = f"{len(rows)}건 · 청산 0건은 순위 제외"
        for rank, (end, direction, timeframe, days, settings, trades, win, ret, mdd, wins, losses) in enumerate(rows, 1):
            side = {"long": "롱", "short": "숏", "both": "양방향"}[direction]
            item = card(TEAL if direction == "long" else PLUM if direction == "short" else BLUE)
            number = rank if trades else "—"
            item.add_widget(make_label(f"{number}위 · {side} · {timeframe} · {days}일 · {end}", size=13, bold=True))
            win_text = f"{win:.1f}%" if win is not None else "—"
            item.add_widget(make_label(f"청산 {trades} (이익 {wins} / 손실 {losses}) · 승률 {win_text}", size=12))
            item.add_widget(make_label(f"평가수익률 {ret:+.2f}% · 최대 낙폭 {mdd:.2f}%", size=12))
            item.add_widget(make_label(settings, color=MUTED, size=11))
            self.history_list.add_widget(item)

    # ── 시스템 이벤트 ──

    def _on_back(self, window, key, *args):
        if key == 27 and self.worker is not None:  # 뒤로 가기: 실행 중에는 앱을 닫지 않음
            self.set_status("실행 중입니다. 종료하려면 중지 버튼을 누르세요.")
            return True
        return False

    def on_pause(self):
        return True  # 홈 버튼으로 나가도 스레드 유지

    def on_stop(self):
        self.stop.set()
        self.wake.set()


if __name__ == "__main__":
    RsiApp().run()
