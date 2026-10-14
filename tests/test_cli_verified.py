from tests.test_cli import _nav, _cli


def test_diff_prints_verified_sha(tmp_path):
    nav, p = _nav(tmp_path)
    _cli(nav, "approve", p.id)
    run = nav.approval_poll()[0]
    assert run.verified_sha
    code, out = _cli(nav, "diff", run.id)
    assert code == 0
    assert f"verified: {run.verified_sha}" in out


def test_diff_for_run_without_verified_sha(tmp_path):
    nav, p = _nav(tmp_path)
    _cli(nav, "approve", p.id)
    run = nav.approval_poll()[0]
    stored = nav.act.runs[run.id]
    stored.verified_sha = None
    nav.act.update_run(stored)
    code, out = _cli(nav, "diff", run.id)
    assert code == 0 and "verified: (not recorded)" in out
