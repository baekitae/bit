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


def test_send_failures_are_swallowed(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    logs = []
    assert Telegram("T", "1", session=Sess(fail=True), log=logs.append).send("x") is False
    assert "네트워크 오류" in logs[-1]
    s401 = Sess(status=401)
    assert Telegram("T", "1", session=s401, log=logs.append).send("x") is False
    assert "토큰" in logs[-1] and len(s401.sent) == 1          # 설정 오류는 재시도하지 않음
    s500 = Sess(status=500)
    assert Telegram("T", "1", session=s500, log=logs.append).send("x") is False
    assert len(s500.sent) == 3                                   # 서버 오류는 3번 시도


def test_send_retries_then_succeeds(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)

    class Flaky(Sess):
        def __init__(self):
            super().__init__()
            self.n = 0

        def post(self, url, json, timeout):
            self.n += 1
            if self.n < 3:
                raise TimeoutError("read timeout")
            return super().post(url, json, timeout)
    f = Flaky()
    assert Telegram("T", "1", session=f, log=lambda s: None).send("x") is True and f.n == 3


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


class UpdSess(Sess):
    """getUpdates 를 흉내 낸다."""

    def __init__(self, updates):
        super().__init__()
        self.updates = updates

    def get(self, url, params, timeout):
        off = params.get("offset")
        ups = [u for u in self.updates if off is None or off == -1 and u is self.updates[-1]
               or (off not in (None, -1) and u["update_id"] >= off)]

        class R:
            status_code = 200

            def json(_):
                return {"ok": True, "result": ups}
        return R()


def upd(i, text, chat="123"):
    return {"update_id": i, "message": {"chat": {"id": int(chat)}, "text": text}}


def test_commands_reply_only_to_owner(conn, cfg):
    from src.notify import Commands
    cfg = make_cfg(cfg)
    run_pair(conn, cfg, FakeLLM())
    conn.execute("INSERT INTO run_log (run_id, created_at, event) VALUES ('t', '2026-01-01 00:00:00', 'comparison_start')")
    sess = UpdSess([upd(1, "/status"), upd(2, "/cost"), upd(3, "/status", chat="999"), upd(4, "/today@KITAE_bot")])
    c = Commands(conn, "t", cfg, Telegram("T", "123", session=sess, log=lambda s: None), now=lambda: T0 + 300 * BAR)
    assert c.poll(0) == 3                                     # 남의 채팅(999)은 무시
    texts = [b["text"] for _, b in sess.sent]
    assert "A1" in texts[0] and "B1" in texts[0] and "비교" in texts[0]
    assert "Claude" in texts[1] and "합계" in texts[1]
    assert "오늘" in texts[2]
    assert c.offset == 5
    assert c.poll(0) == 0                                     # 이미 처리한 명령은 다시 처리 안 함


def test_unknown_command_shows_help_and_backlog_skipped(conn, cfg):
    from src.notify import Commands
    sess = UpdSess([upd(10, "/status"), upd(11, "hello")])
    c = Commands(conn, "t", make_cfg(cfg), Telegram("T", "123", session=sess, log=lambda s: None))
    c.skip_backlog()
    assert c.offset == 12 and c.poll(0) == 0                  # 시작 전 쌓인 명령은 무시
    assert "명령 목록" in c.handle("blah")
    assert c.handle("/status") == "아직 기록이 없습니다."
