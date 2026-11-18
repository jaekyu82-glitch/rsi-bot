"""화면(main.py)과 백그라운드 매매 서비스(service.py) 사이의 연결 통로.

안드로이드 서비스는 화면과 다른 프로세스에서 돌기 때문에, 앱 전용 저장 공간의 파일로
작업 내용·명령·상태·로그를 주고받습니다. (다른 앱은 이 폴더에 접근할 수 없습니다.)
"""

import json
import os
import pickle
import time
import uuid
from pathlib import Path

LOG_LIMIT = 1_000_000      # 로그 파일이 이 크기를 넘으면 뒷부분만 남깁니다.
LOG_KEEP = 200_000
HEARTBEAT_SECONDS = 2
STALE_SECONDS = 20         # 이 시간 동안 신호가 없으면 서비스가 멈춘 것으로 봅니다.


def root_dir(fallback=None):
    """두 프로세스가 같은 폴더를 쓰도록 안드로이드 앱 전용 폴더를 우선 사용합니다."""
    base = os.environ.get("ANDROID_PRIVATE") or os.environ.get("RSIBOT_DATA") or fallback
    if not base:
        raise RuntimeError("작업 폴더를 찾을 수 없습니다.")
    path = Path(base) / "service"
    (path / "commands").mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path, data):
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_bytes(data)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


# ── 작업(서비스가 재시작돼도 이어서 실행할 내용) ──

def save_job(root, job):
    _atomic_write(root / "job.pickle", pickle.dumps(job))


def load_job(root):
    try:
        return pickle.loads((root / "job.pickle").read_bytes())
    except (OSError, pickle.PickleError, EOFError, AttributeError, ImportError):
        return None


def clear_job(root):
    try:
        (root / "job.pickle").unlink()
    except FileNotFoundError:
        pass


# ── 명령 (화면 → 서비스) ──

def send_command(root, *command):
    name = f"{time.time_ns():020d}-{uuid.uuid4().hex}.pickle"
    _atomic_write(root / "commands" / name, pickle.dumps(command))


def take_commands(root):
    folder = root / "commands"
    commands = []
    for path in sorted(folder.glob("*.pickle")):
        try:
            commands.append(pickle.loads(path.read_bytes()))
        except (OSError, pickle.PickleError, EOFError, AttributeError, ImportError):
            pass
        try:
            path.unlink()
        except OSError:
            pass
    return commands


def clear_commands(root):
    for path in (root / "commands").glob("*"):
        try:
            path.unlink()
        except OSError:
            pass


# ── 상태 (서비스 → 화면) ──

def write_status(root, **status):
    status["heartbeat"] = time.time()
    _atomic_write(root / "status.json", json.dumps(status, ensure_ascii=False).encode("utf-8"))


def read_status(root):
    try:
        return json.loads((root / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_alive(status):
    if status is None or status.get("state") not in ("starting", "running"):
        return False
    # 서비스 프로세스가 막 뜨는 중(pid 없음)이면 파이썬 준비 시간을 넉넉히 줍니다.
    limit = STALE_SECONDS if status.get("pid") else STALE_SECONDS * 3
    return time.time() - status.get("heartbeat", 0) < limit


# ── 로그 (서비스 → 화면) ──

def log_path(root):
    return root / "service.log"


def reset_log(root):
    _atomic_write(log_path(root), b"")


def trim_log(root):
    path = log_path(root)
    try:
        if path.stat().st_size <= LOG_LIMIT:
            return
        data = path.read_bytes()[-LOG_KEEP:]
        start = data.find(b"\n") + 1
        _atomic_write(path, data[start:])
    except OSError:
        pass


class LogTail:
    """서비스 로그 파일에서 새로 추가된 줄만 읽습니다."""

    def __init__(self, root):
        self.path = log_path(root)
        self.offset = 0
        self.inode = None

    def reset(self):
        self.offset = 0
        self.inode = None

    def read(self):
        """(처음부터 다시 읽었는지, 새 텍스트)를 돌려줍니다."""
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return False, ""
        restarted = False
        if info.st_ino != self.inode or info.st_size < self.offset:
            restarted = self.inode is not None
            self.inode = info.st_ino
            self.offset = 0
        if info.st_size == self.offset:
            return restarted, ""
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            data = handle.read()
        end = data.rfind(b"\n") + 1   # 줄 단위로만 읽어 한글이 잘리지 않게 합니다.
        if not end:
            return restarted, ""
        self.offset += end
        return restarted, data[:end].decode("utf-8", errors="replace")
