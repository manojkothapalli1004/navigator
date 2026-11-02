from navigator.conductor import telegram

CARD = {"code": "ABC234", "summary": "git push origin main", "reason": "git push"}


def test_card_text_exact():
    assert telegram.card_text(CARD) == ("Approval needed\ngit push origin main\nwhy: git push\n"
                                        "Tap Approve or Reject (or reply  ok ABC234  /  no ABC234). Expires in 60 min.")


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


def test_card_has_approve_and_reject_buttons():
    sent = []
    class R:
        def json(self): return {"ok": True}
    telegram.send_card(CARD, ("T", "42"), post=lambda url, **kw: sent.append(kw["json"]) or R())
    assert sent[0]["reply_markup"] == {"inline_keyboard": [[
        {"text": "Approve", "callback_data": "card:approve:ABC234"},
        {"text": "Reject", "callback_data": "card:decline:ABC234"}]]}


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


# --- overnight 2026-11-02: Telegram counts UTF-16 units; one sender keeps messages in order ---

def _units(s):
    return len(s.encode("utf-16-le")) // 2


def test_split_counts_utf16_units():
    text = "\U0001F600" * 3000  # each emoji is 2 UTF-16 units: 6000 units
    parts = telegram.split_text(text, limit=4000)
    assert "".join(parts) == text and all(_units(p) <= 4000 for p in parts) and len(parts) == 2


def test_split_mixed_prefers_newline_within_units():
    text = "\U0001F600" * 1500 + "\n" + "b" * 2000  # 3000 + 1 + 2000 units
    assert telegram.split_text(text, limit=4000) == ["\U0001F600" * 1500, "b" * 2000]


def test_sender_keeps_order_across_cards_and_texts():
    import threading, time
    sent, done = [], threading.Event()

    class R:
        def json(self): return {"ok": True}

    def post(url, **kw):
        if not sent:
            time.sleep(0.2)  # the first send is slow; later ones must still wait their turn
        sent.append(kw["json"]["text"][:5])
        if len(sent) == 3:
            done.set()
        return R()
    s = telegram.Sender(("T", "1"), post=post)
    s.text("first"); s.card({"code": "ABC234", "summary": "x", "reason": "y"}); s.text("third")
    assert done.wait(3)
    assert sent == ["first", "Appro", "third"]


def test_sender_survives_errors_and_no_creds():
    import threading
    calls, done = [], threading.Event()

    def post(url, **kw):
        calls.append(1)
        if len(calls) == 2:
            done.set()
        raise RuntimeError("down")
    s = telegram.Sender(("T", "1"), post=post)
    s.text("a"); s.text("b")
    assert done.wait(3)
    telegram.Sender(None).text("x")  # no creds: silently nothing
