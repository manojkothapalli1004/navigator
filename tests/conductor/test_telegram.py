from navigator.conductor import telegram

CARD = {"code": "ABC234", "summary": "git push origin main", "reason": "git push"}


def test_card_text_exact():
    assert telegram.card_text(CARD) == ("Approval needed\ngit push origin main\nwhy: git push\n"
                                        "Reply  ok ABC234  to run it, or  no ABC234. Expires in 60 min.")


def test_send_card_never_raises_and_hides_token(capsys):
    def boom(url, **kw):
        raise RuntimeError(f"failed {url}")
    assert telegram.send_card(CARD, ("SECRET-TOKEN", "42"), post=boom) is False
    out = capsys.readouterr()
    assert "SECRET-TOKEN" not in out.out + out.err
    sent = []
    class R:
        def json(self): return {"ok": True}
    assert telegram.send_card(CARD, ("T", "42"), post=lambda url, **kw: sent.append(kw["json"]) or R()) is True
    assert sent[0]["chat_id"] == "42" and "ABC234" in sent[0]["text"]


def test_load_creds_missing_none(tmp_path):
    assert telegram.load_creds(tmp_path / "none.env") is None
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN='t'\n# c\nAUTHORIZED_CHAT_ID=9\n")
    assert telegram.load_creds(tmp_path / ".env") == ("t", "9")


class _OK:
    def json(self): return {"ok": True}


def _rec(sent):
    return lambda url, **kw: sent.append(kw["json"]["text"]) or _OK()


def test_split_text_prefers_newlines():
    assert telegram.split_text("a" * 3000 + "\n" + "b" * 3000, limit=4000) == ["a" * 3000, "b" * 3000]


def test_split_text_hard_cut_without_newline():
    assert [len(p) for p in telegram.split_text("x" * 9000)] == [4000, 4000, 1000]


def test_send_text_splits():
    sent = []
    assert telegram.send_text("x" * 9000, ("T", "1"), post=_rec(sent)) is True
    assert [len(s) for s in sent] == [4000, 4000, 1000]


def test_send_text_never_raises_and_hides_token(capsys, caplog):
    def boom(url, **kw):
        raise RuntimeError(f"failed {url}")
    assert telegram.send_text("hi", ("SECRET-TOKEN", "1"), post=boom) is False
    out = capsys.readouterr()
    assert "SECRET-TOKEN" not in caplog.text + out.out + out.err


def test_send_text_no_creds():
    assert telegram.send_text("hi", None) is False


def test_send_text_empty_sends_placeholder():
    sent = []
    telegram.send_text("", ("T", "1"), post=_rec(sent))
    assert sent == ["(no reply text)"]
