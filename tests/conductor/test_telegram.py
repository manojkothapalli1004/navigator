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
