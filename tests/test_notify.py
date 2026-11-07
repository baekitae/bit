import copy

from src.notify import Notifier, Telegram
from tests.conftest import BAR, T0
from tests.test_council import FakeLLM, run_pair
from tests.test_council import COUNCIL_CFG, REGIME


class Sess:
    def __init__(self, status=200, fail=False):
        self.status, self.fail, self.sent = status, fail, []

    def post(self, url, json, timeout):
        if self.fail:
            raise ConnectionError("down")
        self.sent.append((url, json))

        class R:
            status_code = self.status
        return R()


def make_cfg(cfg):
    cfg["council"] = copy.deepcopy(COUNCIL_CFG)
    cfg["regime"] = REGIME
    return cfg


def test_collects_trades_and_meetings(conn, cfg):
    cfg = make_cfg(cfg)
    sess = Sess()
    n = Notifier(conn, "t", cfg, Telegram("TOKEN", "123", session=sess, log=lambda s: None))
    run_pair(conn, cfg, FakeLLM())                 # A1·B1 진입·청산, 회의 1회
    n.after_step(T0 + 300 * BAR)
    assert len(sess.sent) == 1
    url, body = sess.sent[0]
    assert "botTOKEN/sendMessage" in url and body["chat_id"] == "123"
    text = body["text"]
    assert "A1 진입" in text and "B1 위원회: 따름" in text and "신호 청산" in text
    n.after_step(T0 + 301 * BAR)                   # 새 일이 없으면 보내지 않음
    assert len(sess.sent) == 1


def test_daily_summary_once_per_day(conn, cfg):
    cfg = make_cfg(cfg)
    sess = Sess()
    n = Notifier(conn, "t", cfg, Telegram("T", "1", session=sess, log=lambda s: None))
    run_pair(conn, cfg, FakeLLM())
    n.collect()
    day = T0 + 400 * BAR
    assert n.daily_summary(day) is None            # 시작 직후는 생략
    assert n.daily_summary(day + 3600) is None     # 같은 날
    s = n.daily_summary(day + 86400)
    assert s and "하루 요약" in s and "A1" in s and "누적 AI 비용" in s


def test_send_failures_are_swallowed():
    logs = []
    assert Telegram("T", "1", session=Sess(fail=True), log=logs.append).send("x") is False
    assert Telegram("T", "1", session=Sess(status=401), log=logs.append).send("x") is False
    assert len(logs) == 2


def test_from_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert Telegram.from_env() is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "a")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "b")
    assert Telegram.from_env() is not None


def test_step_error_does_not_kill_loop(conn, tmp_path):
    """회차 처리 중 예외가 나도 루프는 계속되고, 기록이 남는다."""
    from tests.test_live import setup  # noqa: F401
    from src.live import LiveRunner

    class Boom(LiveRunner):
        def __init__(self, conn):
            self.conn, self.run_id, self.notifier = conn, "live", None
            self.boundary, self.cfg = 0, {"live": {"backup_dir": str(tmp_path)}}
            self.log = lambda s: None
            self.now = lambda: 0

        def step(self):
            raise RuntimeError("테스트 오류")
    Boom(conn).safe_step()
    row = conn.execute("SELECT detail FROM run_log WHERE event='step_error'").fetchone()
    assert "테스트 오류" in row[0]
    assert list(tmp_path.glob("bot-*.db"))           # 하루 첫 회차에 백업도 남는다


def test_backup_keeps_latest(conn, tmp_path):
    from src.db import backup_db
    conn.execute("INSERT INTO run_log (run_id, event) VALUES ('x', 'hello')")
    conn.commit()
    for k in range(5):
        backup_db(conn, tmp_path, keep=3, tag=f"2026010{k}")
    files = sorted(p.name for p in tmp_path.glob("bot-*.db"))
    assert files == ["bot-20260102.db", "bot-20260103.db", "bot-20260104.db"]
    import sqlite3
    assert sqlite3.connect(tmp_path / files[-1]).execute("SELECT event FROM run_log").fetchone()[0] == "hello"
