"""BTCUSDT · ETHUSDT RSI 자동매매 핵심 로직 (바이낸스 USDⓈ-M 데모 전용).

원본 btcusdt_rsi_demo.py의 전략·주문·백테스트 코드는 그대로 두고 Windows 전용 부분
(Tk 화면, DPAPI 키 저장, 콘솔 메뉴)만 제거한 모듈입니다. 안드로이드 앱(main.py)이 사용합니다.
"""


from contextlib import closing as close_connection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import hmac
import json
from itertools import chain
from pathlib import Path
from queue import Empty, Full, Queue, SimpleQueue
import sqlite3
import sys
from threading import Event, Thread, RLock
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

AUTHOR = "박재규"
CREATED_ON = "2026년 11월 15일"
PROGRAM_VERSION = "11"
PROGRAM_CREDITS = f"제작자 {AUTHOR} · 제작일 {CREATED_ON} · 버전 {PROGRAM_VERSION}"

BASE = "https://demo-fapi.binance.com"
SYMBOL = "BTCUSDT"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
ORDER_LOCK = RLock()
KEY_LABEL = "820816"
SERVER_TIME = "/fapi/v1/time"
ACCOUNT_INFO = "/fapi/v3/account"
MINUTE_MS = 60_000
TIMEFRAMES = {"3분봉": ("3m", 3), "5분봉": ("5m", 5), "15분봉": ("15m", 15), "1시간봉": ("1h", 60), "4시간봉": ("4h", 240)}
DEFAULT_TIMEFRAME = "5분봉"
CANDLE_INTERVAL, CANDLE_MINUTES = TIMEFRAMES[DEFAULT_TIMEFRAME]
INTERVAL_MS = CANDLE_MINUTES * MINUTE_MS
CANDLE_LIMIT = 500
MAX_WAIT_MINUTES = 480
CONNECTION_CHECK_SECONDS = 60
STATE_PATH = Path(__file__).resolve().with_name("bot_state.sqlite3")
BACKTEST_HISTORY_PATH = STATE_PATH.with_name("backtest_history.sqlite3")
MARGIN_MODE = "/fapi/v1/marginType"
ORDER = "/fapi/v1/order"
ALGO = "/fapi/v1/algoOrder"
POSITION_MODE = "/fapi/v1/positionSide/dual"



def configure_data_dir(directory):
    """안드로이드 앱 전용 저장 폴더로 상태 DB 위치를 옮깁니다."""
    global STATE_PATH, BACKTEST_HISTORY_PATH
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    STATE_PATH = directory / "bot_state.sqlite3"
    BACKTEST_HISTORY_PATH = directory / "backtest_history.sqlite3"
    return directory



class ApiError(RuntimeError):
    def __init__(self, code, message, *, path=None, status=None):
        self.code = code
        details = [f"HTTP {status}" if status is not None else None,
                   f"Binance {code}" if code is not None else None, path, message]
        super().__init__(" · ".join(part for part in details if part))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Binance:
    def __init__(self, key="", secret="", symbol=SYMBOL):
        self.symbol = symbol
        self.key = key
        self.secret = secret.encode()
        self.opener = build_opener(NoRedirect())
        self.offset = 0

    def request(self, method, path, signed=False, **params):
        headers = {"Accept": "application/json", "User-Agent": "BTCUSDT-RSI-Demo/3"}
        if signed:
            params.update(timestamp=int(time.time() * 1000) + self.offset, recvWindow=5000)
            headers["X-MBX-APIKEY"] = self.key
        query = urlencode(params)
        if signed:
            signature = hmac.new(self.secret, query.encode(), hashlib.sha256).hexdigest()
            query += "&signature=" + signature
        body = None
        if method == "POST" and query:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = query.encode("ascii")
            query = ""
        request = Request(BASE + path + ("?" + query if query else ""), data=body,
                          headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                try:
                    result = json.load(response)
                except (ValueError, UnicodeError):
                    raise ApiError(None, "JSON 응답을 받지 못했습니다.", path=path,
                                   status=response.status) from None
                if isinstance(result, dict) and "code" in result and "msg" in result:
                    if result["code"] not in {200, "200"}:
                        raise self.response_error(result, path, response.status)
                return result
        except HTTPError as exc:
            try:
                error = json.load(exc)
            except (ValueError, UnicodeError):
                raise ApiError(None, "JSON 오류 응답을 받지 못했습니다.", path=path,
                               status=exc.code) from None
            raise self.response_error(error, path, exc.code) from None
        except (URLError, TimeoutError, OSError):
            raise ApiError(None, "데모 서버 연결 실패 또는 응답 시간 초과", path=path) from None

    def response_error(self, error, path, status):
        if not isinstance(error, dict):
            return ApiError(None, "예상한 오류 응답 형식이 아닙니다.", path=path, status=status)
        message = str(error.get("msg", "거래소 요청 실패"))
        for value in (self.key, self.secret.decode()):
            if value:
                message = message.replace(value, "[가림]")
        return ApiError(error.get("code"), " ".join(message.split())[:240], path=path, status=status)

    def sync(self):
        start = int(time.time() * 1000)
        response = self.request("GET", SERVER_TIME)
        server = response.get("serverTime") if isinstance(response, dict) else None
        if not isinstance(server, int):
            raise ApiError(None, "서버 시간 응답 형식이 올바르지 않습니다.", path=SERVER_TIME)
        self.offset = server - (start + int(time.time() * 1000)) // 2
        return server

    def account(self, method, path, **params):
        return self.request(method, path, True, **params)

    def account_info(self):
        result = self.account("GET", ACCOUNT_INFO)
        if not isinstance(result, dict) or not isinstance(result.get("assets"), list):
            raise ApiError(None, "데모 선물 계정 정보 응답 형식이 올바르지 않습니다.", path=ACCOUNT_INFO)
        return result

    def positions(self):
        rows = self.account("GET", "/fapi/v3/positionRisk", symbol=self.symbol)
        return [row for row in rows if Decimal(row["positionAmt"]) != 0]

    def open_algos(self):
        return self.account("GET", "/fapi/v1/openAlgoOrders", symbol=self.symbol)

    def hedge_mode(self):
        result = self.account("GET", POSITION_MODE)
        mode = result.get("dualSidePosition") if isinstance(result, dict) else None
        if not isinstance(mode, bool):
            raise ApiError(None, "포지션 모드 응답 형식이 올바르지 않습니다.", path=POSITION_MODE)
        return mode


def rsi_values(closes, period=14):
    """Wilder RSI. Return None for candles before the initial average."""
    if len(closes) <= period:
        return [None] * len(closes)
    moves = [right - left for left, right in zip(closes, closes[1:])]
    gains = [max(move, 0) for move in moves]
    losses = [max(-move, 0) for move in moves]
    gain = sum(gains[:period]) / period
    loss = sum(losses[:period]) / period
    values = [None] * period
    averages = zip(gains[period:], losses[period:])
    for next_gain, next_loss in chain(averages, [(0, 0)]):
        values.append(100 - 100 / (1 + gain / loss) if loss else 100 if gain else 50)
        gain = (gain * (period - 1) + next_gain) / period
        loss = (loss * (period - 1) + next_loss) / period
    return values


@dataclass(frozen=True)
class OrderSettings:
    margin_percent: Decimal = Decimal("10")
    leverage: int = 30
    stop_percent: Decimal = Decimal("30")
    take_percent: Decimal = Decimal("50")


DEFAULT_ORDER = OrderSettings()
DEFAULT_LONG_ORDER = OrderSettings(take_percent=Decimal("25"))


def parse_order_settings(margin_text, leverage_text, stop_text, take_text):
    try:
        margin = Decimal(margin_text)
        leverage = int(leverage_text)
        stop = Decimal(stop_text).copy_abs()
        take = Decimal(take_text)
    except (InvalidOperation, ValueError):
        raise ValueError("잔액 비율·손절·익절은 숫자, 레버리지는 정수로 입력하세요.") from None
    if not all(value.is_finite() for value in (margin, stop, take)):
        raise ValueError("잔액 비율·손절·익절에 유한한 숫자를 입력하세요.")
    if not 0 < margin <= 100:
        raise ValueError("잔액 사용 비율은 0% 초과, 100% 이하로 입력하세요.")
    if not 1 <= leverage <= 125:
        raise ValueError("레버리지는 1~125 사이의 정수로 입력하세요.")
    if not 0 < stop < leverage * 100:
        raise ValueError("손절 폭은 0보다 크고 레버리지 × 100%보다 작아야 합니다.")
    if take <= 0:
        raise ValueError("익절률은 0보다 큰 숫자로 입력하세요.")
    return OrderSettings(margin, leverage, stop, take)


@dataclass(frozen=True)
class EntrySettings:
    first_rsi: float = 20
    wait_minutes: int = CANDLE_MINUTES
    second_rsi: float = 33
    order: OrderSettings = DEFAULT_LONG_ORDER
    direction: str = "long"
    short_settings: "EntrySettings | None" = None
    timeframe: str = DEFAULT_TIMEFRAME


DEFAULT_ENTRY = EntrySettings()
DEFAULT_SHORT_ENTRY = EntrySettings(first_rsi=73, wait_minutes=30, second_rsi=69, order=DEFAULT_ORDER, direction="short")


def entry_confirmed(rsi, threshold, direction):
    return rsi < threshold if direction == "short" else rsi >= threshold


def transition(previous, current, candle, due, settings=DEFAULT_ENTRY, *, direction="long"):
    if due is not None and candle <= due:
        if entry_confirmed(current, settings.second_rsi, direction):
            return None, True
        return (None if candle == due else due), False
    if direction == "short":
        crossed = previous <= settings.first_rsi < current
    else:
        crossed = previous >= settings.first_rsi > current
    if crossed:
        return candle + settings.wait_minutes * MINUTE_MS, False
    return None, False


def side_settings(settings, direction):
    return (settings.short_settings or settings) if direction == "short" else settings


def signal_rules(settings):
    return (settings.direction, settings.timeframe, *(
        (side.first_rsi, side.wait_minutes, side.second_rsi)
        for side in (settings, side_settings(settings, "short"))
    ))


def entry_signals(previous, current, candle, pending, settings):
    signals = []
    for direction in ("long", "short"):
        if settings.direction not in (direction, "both"):
            continue
        pending[direction], triggered = transition(
            previous, current, candle, pending.get(direction),
            side_settings(settings, direction), direction=direction,
        )
        if triggered:
            signals.append(direction)
    return signals


def round_step(value, step, mode=ROUND_FLOOR):
    return (value / step).to_integral_value(rounding=mode) * step


def targets(entry, tick, order=DEFAULT_ORDER, *, legacy=False, short=False):
    # Existing orders retain the price offsets used when their entries were created.
    stop_move = Decimal("0.04") if legacy else order.stop_percent / (100 * order.leverage)
    take_move = Decimal("0.05") if legacy else order.take_percent / (100 * order.leverage)
    if short:
        return (
            round_step(entry * (1 + stop_move), tick),
            round_step(entry * (1 - take_move), tick),
        )
    return (
        round_step(entry * (1 - stop_move), tick, ROUND_CEILING),
        round_step(entry * (1 + take_move), tick, ROUND_CEILING),
    )


def entry_budget(account, order=DEFAULT_ORDER):
    usdt = next((asset for asset in account["assets"] if asset["asset"] == "USDT"), None)
    if usdt is None:
        raise RuntimeError("데모 선물 계정의 USDT 사용 가능 잔액을 확인할 수 없습니다.")
    return Decimal(usdt["availableBalance"]) * order.margin_percent / 100


class DemoTrader:
    def __init__(self, api, db, hedge_mode=False):
        self.hedge_mode = hedge_mode
        self.api = api
        self.symbol = api.symbol
        self.db = db
        db.execute("CREATE TABLE IF NOT EXISTS active (slot INTEGER PRIMARY KEY, entry TEXT, seen_position INTEGER DEFAULT 0)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(active)")}
        if "order_settings" not in columns:
            db.execute("ALTER TABLE active ADD COLUMN order_settings TEXT")
        market = api.request("GET", "/fapi/v1/exchangeInfo")
        info = next(row for row in market["symbols"] if row["symbol"] == self.symbol)
        if info["status"] != "TRADING":
            raise RuntimeError(f"{self.symbol} 데모 거래가 열려 있지 않습니다.")
        self.filters = {row["filterType"]: row for row in info["filters"]}
        self.tick = Decimal(self.filters["PRICE_FILTER"]["tickSize"])

    def active(self):
        return self.db.execute("SELECT slot, entry, order_settings FROM active ORDER BY slot").fetchall()

    def for_position_side(self, rows, side):
        if not self.hedge_mode:
            return rows
        return [row for row in rows if row.get("positionSide") in (side, None, "BOTH")]

    def enter(self, candle, settings=DEFAULT_ORDER, direction="long"):
        with ORDER_LOCK:
            return self._enter(candle, settings, direction)

    def _enter(self, candle, settings=DEFAULT_ORDER, direction="long"):
        api = self.api
        short = direction == "short"
        side = "SHORT" if short else "LONG"
        slot = 2 if self.hedge_mode and short else 1
        position_args = {"positionSide": side} if self.hedge_mode else {}
        tracked = self.active()
        positions = api.positions()
        algos = api.open_algos()
        orders = api.account("GET", "/fapi/v1/openOrders", symbol=self.symbol)
        if (any(not self.hedge_mode or row[0] == slot or row[1].startswith("r1s-") == short for row in tracked)
                or self.for_position_side(positions + algos + orders, side)):
            print(f"기존 {side} 거래 또는 주문이 있어 진입을 건너뜁니다.", flush=True)
            return
        margin = entry_budget(api.account_info(), settings)
        if margin <= 0:
            print("사용 가능한 데모 USDT 잔액이 없어 진입을 건너뜁니다.", flush=True)
            return
        if self.hedge_mode and (tracked or positions or algos or orders):
            configs = api.account("GET", "/fapi/v1/symbolConfig", symbol=self.symbol)
            config = next((row for row in configs if row["symbol"] == self.symbol), None)
            if (config is None or config["marginType"] != "ISOLATED"
                    or int(config["leverage"]) != settings.leverage):
                print(f"기존 {self.symbol} 포지션 또는 주문과 격리·레버리지 설정이 달라 진입을 건너뜁니다.", flush=True)
                return
            max_notional = Decimal(config["maxNotionalValue"])
        else:
            try:
                api.account("POST", MARGIN_MODE, symbol=self.symbol, marginType="ISOLATED")
            except ApiError as exc:
                if exc.code != -4046:
                    raise
            leverage = api.account("POST", "/fapi/v1/leverage", symbol=self.symbol, leverage=settings.leverage)
            if int(leverage["leverage"]) != settings.leverage:
                raise RuntimeError(f"{settings.leverage}배 레버리지 설정을 확인할 수 없습니다.")
            max_notional = Decimal(leverage["maxNotionalValue"])

        price_field = "bidPrice" if short else "askPrice"
        price = Decimal(api.request("GET", "/fapi/v1/ticker/bookTicker", symbol=self.symbol)[price_field])
        market_lot = self.filters["MARKET_LOT_SIZE"]
        step = max(Decimal(market_lot["stepSize"]), Decimal(self.filters["LOT_SIZE"]["stepSize"]))
        qty = round_step(margin * settings.leverage / price, step)
        if not Decimal(market_lot["minQty"]) <= qty <= Decimal(market_lot["maxQty"]):
            print(f"주문 수량이 {self.symbol} 시장가 한도를 벗어나 진입을 건너뜁니다.", flush=True)
            return
        if qty * price < Decimal(self.filters["MIN_NOTIONAL"]["notional"]):
            print(f"{self.symbol} 최소 주문 금액보다 작아 진입을 건너뜁니다.", flush=True)
            return
        existing_notional = sum(abs(Decimal(row["notional"])) for row in positions)
        pending_notional = sum(
            (Decimal(row["origQty"]) - Decimal(row["executedQty"])) * max(price, Decimal(row["price"]))
            for row in orders
        )
        if qty * price + existing_notional + pending_notional > max_notional:
            print(f"{settings.leverage}배 레버리지의 주문 한도를 초과해 진입을 건너뜁니다.", flush=True)
            return
        print(f"데모 {'숏' if short else '롱'} 진입 요청 · 목표 증거금 {margin:.2f} USDT · 수량 {qty} {self.symbol.removesuffix('USDT')}", flush=True)
        entry = f"{'r1s' if short else 'r1'}-{candle}-{uuid4().hex[:8]}"
        order_settings = json.dumps([
            str(settings.margin_percent), str(settings.leverage),
            str(settings.stop_percent), str(settings.take_percent),
        ])
        self.db.execute(
            "INSERT INTO active (slot, entry, order_settings) VALUES (?, ?, ?)",
            (slot, entry, order_settings),
        )
        self.db.commit()
        try:
            api.account("POST", ORDER, symbol=self.symbol, side="SELL" if short else "BUY", type="MARKET",
                        quantity=format(qty, "f"), newClientOrderId=entry, newOrderRespType="RESULT", **position_args)
        except ApiError as exc:
            print(f"진입 응답 재확인 중 · {exc} · {entry}", flush=True)
        for attempt in range(5):
            try:
                self.manage_position(slot, entry, order_settings)
                return
            except ApiError:
                if attempt == 4:
                    raise
                time.sleep(1)

    def manage(self):
        error = None
        for row in self.active():
            try:
                self.manage_position(*row)
            except ApiError as exc:
                error = exc
        if error is not None:
            raise error

    def manage_position(self, slot, entry, saved_settings):
        short = entry.startswith("r1s-")
        side = "SHORT" if short else "LONG"
        position_args = {"positionSide": side} if self.hedge_mode else {}
        close_side = "BUY" if short else "SELL"
        direction_sign = -1 if short else 1
        stored = json.loads(saved_settings) if saved_settings else []
        settings = parse_order_settings(*stored[:4]) if stored else DEFAULT_ORDER
        api = self.api
        fill = api.account("GET", ORDER, symbol=self.symbol, origClientOrderId=entry)
        executed = Decimal(fill["executedQty"])
        positions = [row for row in api.positions() if not self.hedge_mode or row["positionSide"] == side]
        prefix = entry + "-"
        orders = {
            row["clientAlgoId"]: row for row in api.open_algos()
            if row["clientAlgoId"].startswith(prefix)
        }
        if not positions:
            if fill["status"] in {"NEW", "PARTIALLY_FILLED"}:
                return
            seen = self.db.execute("SELECT seen_position FROM active WHERE slot = ?", (slot,)).fetchone()[0]
            if executed > 0 and not seen:
                return
            for client_id in orders:
                api.account("DELETE", ALGO, clientAlgoId=client_id)
            self.db.execute("DELETE FROM active WHERE slot = ?", (slot,))
            self.db.commit()
            print("포지션 종료 확인 및 남은 청산 주문 정리 완료.", flush=True)
            return

        position = positions[0]
        amount = direction_sign * Decimal(position["positionAmt"])
        if len(positions) != 1 or amount <= 0 or amount > executed:
            raise RuntimeError(f"기록과 다른 {self.symbol} 포지션입니다. 데모 계정에서 확인하세요.")
        average = Decimal(fill["avgPrice"])
        if average <= 0:
            raise RuntimeError("평균 체결가를 확인할 수 없습니다.")
        self.db.execute("UPDATE active SET seen_position = 1 WHERE slot = ? AND seen_position = 0", (slot,))
        self.db.commit()
        stop, take = targets(average, self.tick, settings, legacy=entry.startswith("rsi-"), short=short)
        # Recover the original stop distance for positions opened by the previous version.
        if len(stored) == 5:
            distance = Decimal(stored[4])
            if short:
                stop = round_step(average + distance, self.tick, ROUND_CEILING)
            else:
                stop = round_step(average - distance, self.tick)
        for label, kind, trigger in (("sl", "STOP_MARKET", stop), ("tp", "TAKE_PROFIT_MARKET", take)):
            client_id = prefix + label
            if client_id in orders:
                continue
            current = [row for row in api.positions() if not self.hedge_mode or row["positionSide"] == side]
            if not current:
                return
            try:
                api.account("POST", ALGO, algoType="CONDITIONAL", symbol=self.symbol,
                            side=close_side, type=kind, triggerPrice=format(trigger, "f"),
                            workingType="MARK_PRICE", closePosition="true", clientAlgoId=client_id, **position_args)
                print(f"{side} {label.upper()} 데모 청산 주문 접수 · 평균 체결가 {average} · 트리거 {trigger}", flush=True)
            except ApiError as exc:
                if any(row["clientAlgoId"] == client_id for row in api.open_algos()):
                    continue
                # If a protective trigger cannot be installed, flatten only this bot's demo position.
                remaining = [row for row in api.positions() if not self.hedge_mode or row["positionSide"] == side]
                if remaining:
                    residual = direction_sign * Decimal(remaining[0]["positionAmt"])
                    if len(remaining) != 1 or not 0 < residual <= executed:
                        raise RuntimeError("긴급 청산 전 데모 포지션 확인이 필요합니다.") from exc
                    api.account("POST", ORDER, symbol=self.symbol, side=close_side, type="MARKET",
                                quantity=format(residual, "f"), newOrderRespType="RESULT",
                                **(position_args if self.hedge_mode else {"reduceOnly": "true"}))
                    print(f"청산 주문 등록 실패로 데모 포지션 종료 요청 · {exc}", flush=True)
                return



def show_entry_settings(settings):
    for direction, label in (("long", "롱"), ("short", "숏")):
        if settings.direction not in (direction, "both"):
            continue
        side = side_settings(settings, direction)
        if direction == "short":
            trigger = f"{side.first_rsi:g} 초과 상향 돌파 후 최대 {side.wait_minutes}분 동안 진행 중 RSI {side.second_rsi:g} 미만 시 진입"
        else:
            trigger = f"{side.first_rsi:g} 미만 하향 돌파 후 최대 {side.wait_minutes}분 동안 진행 중 RSI {side.second_rsi:g} 이상 시 진입"
        order = side.order
        print(f"{label} 진입 · {settings.timeframe} RSI(14) {trigger}", flush=True)
        print(
            f"{label} 주문 · 사용 가능 잔액 {order.margin_percent}% · 격리 {order.leverage}배 · "
            f"손절 −{order.stop_percent}% · 익절 +{order.take_percent}% (증거금 대비)",
            flush=True,
        )


def live_candle_rsi(candles, closed, server, interval_ms):
    current = candles[-1]
    if (current[0] != closed[-1][0] + interval_ms
            or not current[0] <= server <= current[6]):
        return None
    prices = [float(row[4]) for row in closed]
    rsi = rsi_values([*prices, float(current[4])])[-1]
    return current[0], rsi


def watch(api, trader=None, settings=DEFAULT_ENTRY, *, stop=None, updates=None, manual_orders=None, wake=None):
    stop = stop if stop is not None else Event()
    wait = wake.wait if wake is not None else stop.wait
    show_entry_settings(settings)
    last = None
    after_close = None
    consumed = {}
    next_account_check = time.monotonic() + CONNECTION_CHECK_SECONDS
    while not stop.is_set():
        if wake is not None:
            wake.clear()
        try:
            server = api.sync()
            if updates is not None:
                try:
                    changed = updates.get_nowait()
                except Empty:
                    pass
                else:
                    while not updates.empty():
                        changed = updates.get_nowait()
                    if changed != settings:
                        if signal_rules(changed) != signal_rules(settings):
                            after_close = server
                            consumed.clear()
                            last = None
                            print("RSI 조건 변경 · 이전 진입 대기를 취소했습니다.", flush=True)
                        settings = changed
                        print("새 조건 적용 · 다음 신규 진입부터 사용합니다.", flush=True)
                        show_entry_settings(settings)
            if trader and time.monotonic() >= next_account_check:
                api.account_info()
                if api.hedge_mode() != trader.hedge_mode:
                    raise RuntimeError("데모 계정의 포지션 모드가 변경되었습니다. 설정을 확인한 뒤 다시 시작하세요.")
                print("계정 상태 · 데모 API 인증 확인 완료", flush=True)
                next_account_check = time.monotonic() + CONNECTION_CHECK_SECONDS
            if trader:
                trader.manage()
                if manual_orders is not None and not stop.is_set():
                    try:
                        direction, order, deadline = manual_orders.get_nowait()
                    except Empty:
                        pass
                    else:
                        if api.hedge_mode() != trader.hedge_mode:
                            raise RuntimeError("데모 계정의 포지션 모드가 변경되었습니다. 다시 시작하세요.")
                        if time.monotonic() > deadline:
                            print("시장가 진입 요청 시간이 지나 취소했습니다. 다시 눌러주세요.", flush=True)
                        elif not stop.is_set():
                            trader.enter(server, order, direction)
            interval, minutes = TIMEFRAMES[settings.timeframe]
            candles = api.request("GET", "/fapi/v1/klines", symbol=api.symbol, interval=interval, limit=CANDLE_LIMIT)
            closed = [candle for candle in candles if candle[6] < server]
            if len(closed) < 16:
                wait(5)
                continue
            interval_ms = minutes * MINUTE_MS
            latest = closed[-1][0]
            is_new_candle = latest != last
            timely = (last is not None and latest - last == interval_ms
                      and server - closed[-1][6] <= MINUTE_MS)
            if after_close is None:
                # Begin with the next newly completed candle, without replaying old setups.
                after_close = closed[-1][6] + 1
            values = rsi_values([float(candle[4]) for candle in closed])
            pending = {}
            signals = []
            previous_due = {}
            for index in range(15, len(closed)):
                if closed[index][6] < after_close:
                    continue
                previous_due = pending.copy()
                signals = entry_signals(values[index - 1], values[index], closed[index][0], pending, settings)
            eligible = {
                direction: deadline for direction, deadline in pending.items()
                if deadline is not None and deadline != consumed.get(direction)
            }
            if is_new_candle:
                waiting = ", ".join(
                    f"{'롱' if direction == 'long' else '숏'} {(deadline-latest)//MINUTE_MS}분"
                    for direction, deadline in eligible.items()
                ) or "없음"
                print(f"{api.symbol} {settings.timeframe} 마감 RSI {values[-1]:.4f} · 대기 {waiting}", flush=True)
            last = latest
            live = live_candle_rsi(candles, closed, server, interval_ms) if eligible else None
            candidates = []
            for direction in ("long", "short"):
                label = "롱" if direction == "long" else "숏"
                side = side_settings(settings, direction)
                deadline = eligible.get(direction)
                if (deadline is not None and live and live[0] <= deadline
                        and entry_confirmed(live[1], side.second_rsi, direction)):
                    candle, rsi = live
                    consumed[direction] = deadline
                    print(f"{api.symbol} {settings.timeframe} 진행 중 RSI {rsi:.4f} · {label} 진입 신호", flush=True)
                    candidates.append((candle, direction))
                elif timely and direction in signals and previous_due.get(direction) != consumed.get(direction):
                    consumed[direction] = previous_due[direction]
                    print(f"{api.symbol} {settings.timeframe} 마감 RSI {values[-1]:.4f} · {label} 진입 신호", flush=True)
                    candidates.append((latest, direction))
            if candidates and trader:
                if settings.direction == "both" and not trader.hedge_mode:
                    print("양방향 진입에는 데모 계정의 헤지 모드가 필요합니다.", flush=True)
                else:
                    for candle, signal in candidates:
                        if stop.is_set() or (updates is not None and not updates.empty()):
                            break
                        trader.enter(candle, side_settings(settings, signal).order, signal)
        except ApiError as exc:
            if trader:
                last = None
                next_account_check = 0
                print("계정 상태 · 데모 연결 재확인 중", flush=True)
            print(f"거래소 상태 재확인 중 · {exc}", file=sys.stderr, flush=True)
            stop.wait(15)
            continue
        wait(2 if any(deadline != consumed.get(side) for side, deadline in eligible.items()) else 5)


def self_test():
    due, signal = transition(20, 19, 0, None)
    assert (due, signal) == (INTERVAL_MS, False)
    assert transition(21, 20, 0, None) == (None, False)
    assert transition(19, 33.1, INTERVAL_MS, due) == (None, True)
    assert transition(19, 33, INTERVAL_MS, due) == (None, True)
    assert transition(19, 32.9, INTERVAL_MS, due) == (None, False)
    assert transition(19, 34, 2 * INTERVAL_MS, due) == (None, False)
    pending = {}
    short = DEFAULT_SHORT_ENTRY
    assert entry_signals(72, 73, 0, pending, short) == []
    assert entry_signals(73, 74, INTERVAL_MS, pending, short) == []
    deadline = INTERVAL_MS + 30 * MINUTE_MS
    assert pending["short"] == deadline
    assert entry_signals(74, 69, 2 * INTERVAL_MS, pending, short) == []
    assert pending["short"] == deadline
    assert entry_signals(69, 68.9, 3 * INTERVAL_MS, pending, short) == ["short"]
    assert pending["short"] is None
    assert transition(71, 68.9, deadline, deadline, short, direction="short") == (None, True)
    assert transition(71, 69, deadline, deadline, short, direction="short") == (None, False)
    assert transition(71, 68.9, deadline + INTERVAL_MS, deadline, short, direction="short") == (None, False)
    entry = Decimal("100000")
    tick = Decimal("0.1")
    assert targets(entry, tick) == (Decimal("99000"), Decimal("101666.7"))
    assert targets(entry, tick, legacy=True) == (Decimal("96000"), Decimal("105000"))
    assert entry_budget({"assets": [
        {"asset": "USDC", "availableBalance": "1000"},
        {"asset": "USDT", "availableBalance": "100", "walletBalance": "200"},
    ]}) == Decimal("10")
    custom = EntrySettings(25.5, 10, 12.5)
    custom_due = 2 * INTERVAL_MS
    assert transition(26, 25, 0, None, custom) == (custom_due, False)
    assert transition(14, 12, INTERVAL_MS, custom_due, custom) == (custom_due, False)
    assert transition(14, 12.5, custom_due, custom_due, custom) == (None, True)
    assert transition(14, 13, custom_due, custom_due, custom) == (None, True)
    assert len(rsi_values(list(range(20)))) == 20
    assert rsi_values(list(range(20)))[-1] == 100
    assert rsi_values(list(range(20, 0, -1)))[-1] == 0
    print("로컬 전략 및 가격 계산 검증 통과.", flush=True)



def validate_key(raw, label):
    # Ignore clipboard padding around the value, never change its interior.
    value = raw.strip().strip("\ufeff\u200b").strip()
    if not value or value == KEY_LABEL:
        raise ValueError(f"{label} · 발급 화면의 실제 키 값을 복사하세요.")
    if any(not ("!" <= character <= "~") for character in value):
        raise ValueError(f"{label} · 키 값 안에 공백 또는 숨은 문자가 있습니다. 발급 화면에서 값만 다시 복사하세요.")
    return value



def authenticate_demo(key, secret):
    api = Binance(key, secret)
    print("2/3 · 데모 선물 계정 정보 V3 인증 중", flush=True)
    try:
        api.sync()
    except ApiError as exc:
        raise RuntimeError(f"서버 시간 재동기화 실패 · 키를 보내기 전 단계 · {exc}") from None
    try:
        api.account_info()
    except ApiError as exc:
        raise RuntimeError(f"데모 계정 정보 인증 실패 · {exc}") from None
    print("2/3 · 데모 API 인증 성공", flush=True)
    try:
        hedge_mode = api.hedge_mode()
    except ApiError as exc:
        raise RuntimeError(f"3/3 · 계정 인증은 성공했으나 포지션 모드 확인 실패 · {exc}") from None
    position_mode = "헤지" if hedge_mode else "단방향"
    print(f"3/3 · 포지션 모드 {position_mode}", flush=True)
    return api, hedge_mode



def run_demo(api, hedge_mode, settings, *, stop=None, updates=None,
             manual_orders=None, manual_order=None, wake=None):
    if manual_order is None and settings.direction == "both" and not hedge_mode:
        raise RuntimeError("양방향 진입을 사용하려면 바이낸스 데모 계정을 헤지 모드로 설정하세요.")

    state_path = STATE_PATH if api.symbol == SYMBOL else STATE_PATH.with_name(f"bot_state_{api.symbol}.sqlite3")
    with sqlite3.connect(str(state_path) + ".lock", timeout=0) as lock:
        lock.execute("BEGIN EXCLUSIVE")
        with sqlite3.connect(state_path) as db:
            trader = DemoTrader(api, db, hedge_mode)
            if manual_order is not None:
                if stop is None or not stop.is_set():
                    direction, order = manual_order
                    server = api.sync()
                    if stop is None or not stop.is_set():
                        trader.enter(server, order, direction)
                return
            print(f"실행 모드 · 데모 자동매매 · {api.symbol}", flush=True)
            watch(api, trader, settings, stop=stop, updates=updates, manual_orders=manual_orders, wake=wake)



def run_symbols(api, hedge_mode, settings, symbols, stop, updates, manual_orders, wake):
    """Each market owns its API client and database. Entries share one budget lock."""
    errors = SimpleQueue()
    feeds = {symbol: SimpleQueue() for symbol in symbols}
    orders = {symbol: Queue(maxsize=1) for symbol in symbols}
    workers = []

    def run_symbol(symbol):
        client = Binance(api.key, api.secret.decode(), symbol=symbol)
        try:
            run_demo(client, hedge_mode, settings, stop=stop, updates=feeds[symbol],
                     manual_orders=orders[symbol], wake=wake)
        except Exception as exc:
            errors.put(exc)
            stop.set()
            wake.set()

    try:
        for symbol in symbols:
            thread = Thread(target=run_symbol, args=(symbol,))
            thread.start()
            workers.append(thread)
        while not stop.wait(0.1):
            while not updates.empty():
                changed = updates.get_nowait()
                for feed in feeds.values():
                    feed.put(changed)
            try:
                symbol, direction, order, deadline = manual_orders.get_nowait()
            except Empty:
                continue
            if symbol not in orders:
                print(f"{symbol} 감시가 실행 중이지 않아 진입 요청을 취소했습니다.", flush=True)
                continue
            try:
                orders[symbol].put_nowait((direction, order, deadline))
            except Full:
                print(f"{symbol} 앞선 진입 요청을 처리 중입니다.", flush=True)
            wake.set()
    finally:
        stop.set()
        wake.set()
        for thread in workers:
            thread.join()
    if not errors.empty():
        raise errors.get_nowait()


def historical_candles(api, path, start, end, stop, timeframe=DEFAULT_TIMEFRAME):
    interval, minutes = TIMEFRAMES[timeframe]
    rows = []
    while start < end:
        if stop.is_set():
            raise RuntimeError("백테스트를 취소했습니다.")
        batch = api.request("GET", path, symbol=api.symbol, interval=interval,
                            startTime=start, endTime=end-1, limit=1000)
        if not batch or batch[0][0] >= end:
            raise RuntimeError("요청한 기간의 과거 캔들이 부족합니다. 기간을 줄여보세요.")
        for row in batch:
            if row[0] >= end:
                break
            if row[0] != start:
                raise RuntimeError("과거 캔들에 누락이 있어 백테스트를 중단했습니다.")
            rows.append(row)
            start += minutes * MINUTE_MS
    return rows


def simulate_backtest(candles, marks, start, settings, tick, step, minimum, min_notional, *, daily=None):
    values = rsi_values([float(row[4]) for row in candles])
    balance = initial = Decimal("1000")
    peak = initial
    drawdown = Decimal(0)
    positions = {}
    pending = {}
    signals = []
    wins = losses = flat = 0
    for i in range(15, len(candles)):
        candle = candles[i]
        timestamp = candle[0]
        if timestamp < start:
            continue
        op, close = Decimal(candle[1]), Decimal(candle[4])
        for signal in signals:
            if signal in positions:
                continue
            order = side_settings(settings, signal).order
            other_leverages = {side_settings(settings, direction).order.leverage for direction in positions}
            if other_leverages and order.leverage not in other_leverages:
                continue
            reserved = sum(
                entry * qty / side_settings(settings, direction).order.leverage
                for direction, (entry, qty, *_) in positions.items()
            )
            available = max(Decimal(0), balance - reserved)
            nominal = available * order.margin_percent / 100 * order.leverage
            qty = round_step(nominal / op, step)
            if qty >= minimum and qty * op >= min_notional:
                short = signal == "short"
                stop_price, take_price = targets(op, tick, order, short=short)
                positions[signal] = (op, qty, stop_price, take_price, short)
        for direction, (entry, qty, stop_price, take_price, short) in list(positions.items()):
            lower, upper = sorted((stop_price, take_price))
            mark_open, high, low = map(Decimal, marks[i][1:4])
            # An opening gap takes priority. When both levels are touched later,
            # the candle cannot tell us the order, so assume the stop came first.
            exit_price = None
            if mark_open <= lower or mark_open >= upper:
                exit_price = op
            elif (high >= stop_price if short else low <= stop_price):
                exit_price = stop_price
            elif (low <= take_price if short else high >= take_price):
                exit_price = take_price
            if exit_price is not None:
                pnl = (exit_price - entry) * qty * (-1 if short else 1)
                balance += pnl
                if daily is not None:
                    # The exact exit minute is unknown within a historical candle.
                    date = korean_date(timestamp)
                    amounts = daily.setdefault(date, [Decimal(0), Decimal(0)])
                    amounts[0 if pnl >= 0 else 1] += pnl
                wins += pnl > 0
                losses += pnl < 0
                flat += pnl == 0
                del positions[direction]
        signals = entry_signals(values[i-1], values[i], timestamp, pending, settings)
        equity = balance + sum((close-entry) * qty * (-1 if short else 1)
                               for entry, qty, _, _, short in positions.values())
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak * 100)
    return balance, equity, wins, losses, flat, drawdown, len(positions)


def save_backtest_result(settings, days, end, result, symbol=SYMBOL):
    _, equity, wins, losses, flat, drawdown, _ = result
    side = side_settings(settings, settings.direction)
    order = side.order
    wait_label = f"최대 {side.wait_minutes}분"
    description = (
        f"{symbol} RSI {side.first_rsi:g} → {wait_label} → {side.second_rsi:g} / "
        f"잔액 {order.margin_percent}% / {order.leverage}배 / "
        f"손절 −{order.stop_percent}% / 익절 +{order.take_percent}%"
    )
    korea = timezone(timedelta(hours=9))
    period_end = datetime.fromtimestamp(end / 1000, korea).strftime("%Y-%m-%d %H:%M")
    with close_connection(sqlite3.connect(BACKTEST_HISTORY_PATH)) as db, db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS backtest_results (
                id INTEGER PRIMARY KEY, period_end TEXT NOT NULL, direction TEXT NOT NULL,
                timeframe TEXT NOT NULL, days INTEGER NOT NULL, settings TEXT NOT NULL,
                wins INTEGER NOT NULL, losses INTEGER NOT NULL, flat INTEGER NOT NULL,
                return_percent REAL NOT NULL, drawdown_percent REAL NOT NULL
            )
        """)
        db.execute("""
            INSERT INTO backtest_results
                (period_end, direction, timeframe, days, settings, wins, losses, flat,
                 return_percent, drawdown_percent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (period_end, settings.direction, settings.timeframe, days, description,
              wins, losses, flat, float(equity / 10 - 100), float(drawdown)))


def ranked_backtests(sort_by):
    if not BACKTEST_HISTORY_PATH.exists():
        return []
    orders = {
        "승률순": "win_rate DESC, return_percent DESC",
        "평가수익률순": "return_percent DESC, win_rate DESC",
    }
    with close_connection(sqlite3.connect(BACKTEST_HISTORY_PATH)) as db:
        return db.execute(f"""
            SELECT period_end, direction, timeframe, days, settings, wins + losses + flat AS trades,
                   100.0 * wins / NULLIF(wins + losses + flat, 0) AS win_rate,
                   return_percent, drawdown_percent, wins, losses
            FROM backtest_results
            ORDER BY (trades = 0), {orders[sort_by]}, id DESC
        """).fetchall()


KOREA_TIME = timezone(timedelta(hours=9))


def korean_date(timestamp):
    return datetime.fromtimestamp(timestamp / 1000, KOREA_TIME).date()


def run_backtest(settings, days, stop, symbol=SYMBOL):
    api = Binance(symbol=symbol)
    interval_ms = TIMEFRAMES[settings.timeframe][1] * MINUTE_MS
    end = api.sync() // interval_ms * interval_ms
    start = end - days * 1440 * MINUTE_MS
    print(f"백테스트 · {api.symbol} 데모 과거 {days}일 · 가상 시작 잔액 1,000 USDT", flush=True)
    show_entry_settings(settings)
    print("백테스트는 봉 마감 RSI로 근사합니다. 진행 중에만 발생한 신호는 포함되지 않습니다.", flush=True)
    print(f"과거 거래가 {settings.timeframe} 다운로드 중", flush=True)
    candles = historical_candles(api, "/fapi/v1/klines", start-500*interval_ms, end, stop, settings.timeframe)
    print(f"과거 표시가격 {settings.timeframe} 다운로드 중", flush=True)
    marks = historical_candles(api, "/fapi/v1/markPriceKlines", start-500*interval_ms, end, stop, settings.timeframe)
    info = api.request("GET", "/fapi/v1/exchangeInfo")
    symbol = next(row for row in info["symbols"] if row["symbol"] == api.symbol)
    filters = {row["filterType"]: row for row in symbol["filters"]}
    lot = filters["MARKET_LOT_SIZE"]
    step = max(Decimal(lot["stepSize"]), Decimal(filters["LOT_SIZE"]["stepSize"]))
    daily = {}
    result = simulate_backtest(candles, marks, start, settings,
                               Decimal(filters["PRICE_FILTER"]["tickSize"]), step,
                               Decimal(lot["minQty"]), Decimal(filters["MIN_NOTIONAL"]["notional"]), daily=daily)
    balance, equity, wins, losses, flat, drawdown, open_position = result
    count = wins + losses + flat
    print(f"청산 완료 {count}건 · 이익 {wins} · 손실 {losses} · 보합 {flat}", flush=True)
    if count:
        print(f"승률 {wins/count:.1%}", flush=True)
    print(f"실현 잔액 {balance:,.2f} USDT · 종료 평가액 {equity:,.2f} USDT · 평가 수익률 {equity/10-100:+.2f}%", flush=True)
    print(f"분봉 마감 기준 최대 낙폭 {drawdown:.2f}% · 종료 시 보유 포지션 {int(open_position)}건", flush=True)
    print("다음 봉 시가 진입 가정 · 한 봉에서 두 가격에 닿으면 손절 우선 · 수수료·펀딩·슬리피지·강제청산 제외", flush=True)

    if not stop.is_set():
        try:
            save_backtest_result(settings, days, end, result, api.symbol)
        except (OSError, sqlite3.Error) as exc:
            print(f"백테스트 기록 저장 실패 · {exc}", flush=True)
        else:
            print("백테스트 기록 저장 완료", flush=True)

    dates = (korean_date(start), korean_date(end - 1))
    return [
        (date.isoformat(), *daily.get(date, (Decimal(0), Decimal(0))))
        for index in range((dates[1] - dates[0]).days + 1)
        for date in (dates[0] + timedelta(days=index),)
    ]

