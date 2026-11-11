import asyncio
from pathlib import Path

import pytest

from navigator.conductor.gate import can_use_tool, gated_reason, pre_tool_use_hook

CWD = Path("/Users/Manoj/calwbot")
HOME = Path.home()


@pytest.mark.parametrize("cmd", [
    "git merge x", "git -C /r merge x", "git -c a=b push", "cd x && git rebase main",
    'bash -c "git reset --hard"', "git reset --hard HEAD~1", "git commit --amend -m x",
    "git filter-branch --all", "launchctl bootout gui/504/x", "launchctl load ~/Library/LaunchAgents/a.plist",
    "cat trader/state.json > /dev/null", "ls; git push origin main", "echo hi | git merge y",
    "cp a.plist ~/Library/LaunchAgents/", "git update-ref refs/heads/main abc",
])
def test_bash_gated(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD)


@pytest.mark.parametrize("cmd", ["git status", "git log --oneline", "ls", "grep merge README.md",
                                 "git diff main..x", "launchctl list", "git reset HEAD file.py"])
def test_bash_not_gated(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD) is None


@pytest.mark.parametrize("path", [str(HOME / ".claude/settings.json"), "CLAUDE.md", "navigator/../trader/x.py",
                                  str(HOME / "Library/LaunchAgents/a.plist"), "x/settings.local.json",
                                  str(HOME / ".claude/hooks/x.sh")])
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_write_gated(tool, path):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    assert gated_reason(tool, {key: path}, CWD)


def test_write_not_gated():
    assert gated_reason("Write", {"file_path": "docs/x.md"}, CWD) is None
    assert gated_reason("Read", {"file_path": "trader/x.py"}, CWD) is None
    # Claude's own memory notes are normal work, not permissions
    assert gated_reason("Write", {"file_path": str(HOME / ".claude/projects/m/memory/x.md")}, CWD) is None
    assert gated_reason("Write", {"file_path": str(HOME / ".claude/projects/m/memory/settings.json")}, CWD)


def test_hook_shape_deny():
    out = asyncio.run(pre_tool_use_hook({"tool_name": "Bash", "tool_input": {"command": "git merge x"},
                                         "cwd": str(CWD)}, "t1", None))
    h = out["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse" and h["permissionDecision"] == "deny"
    assert "operator" in h["permissionDecisionReason"]
    assert asyncio.run(pre_tool_use_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "t2", None)) == {}


def test_can_use_tool_denies():
    r = asyncio.run(can_use_tool("Bash", {"command": "rm -rf x"}, None))
    assert r.behavior == "deny" and "operator" in r.message


@pytest.mark.parametrize("cmd", [
    # review C1/C2: the workspace's own merge path and Bash writes to protected files
    ".venv/bin/python -m navigator merge run-1", "navigator reject run-2",
    'cat > CLAUDE.md <<EOF\nx\nEOF', 'sed -i "" s/a/b/ ~/.claude/settings.json', "echo x >> ~/.claude.json",
    "cp x .mcp.json", "echo x > .git/hooks/pre-commit", "vim .claude/hooks/a.sh",
    # review I3: more git forms, wrappers and gh
    "git pull", "git cherry-pick abc", "git branch -f main abc", "git branch -D x", "git checkout -B main x",
    "git switch -C main", "git reset --keep HEAD~1", "git reset HEAD~3", "nohup git merge x",
    "time git push", "xargs git merge < f", "(git merge x)", "{ git merge x; }", "if true; then git merge x; fi",
    "bash -lc 'git merge x'", "sh -ec 'git push'", "echo git merge x | sh", "gh repo create x --push", "gh pr create",
])
def test_bash_gated_review(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD)


@pytest.mark.parametrize("path", [str(HOME / ".claude.json"), ".mcp.json", ".git/hooks/pre-commit",
                                  ".claude/hooks/a.sh", "navigator/.claude/agents/x.md"])
def test_write_gated_review(path):
    assert gated_reason("Write", {"file_path": path}, CWD)


# ---- 2026-11-02: read-only looks at ~/.claude/plugins (skill helper files) pass; writes stay gated ----

S = "~/.claude/plugins/cache/claude-plugins-official/superpowers/7.0.0/skills"
# the 2026-11-01 C4 command; its `cd … && git …` part now stays gated (a cd'd-to repo's own config
# can run programs), so the read-only part is what must pass
C4_LS = (f"git status --short | head -5; git branch --show-current; "
         f"git worktree list | head; S=/Users/Manoj/.claude/plugins/cache/claude-plugins-official/superpowers/"
         f"7.0.0/skills; ls $S/executing-plans/scripts $S/subagent-driven-development/scripts")


@pytest.mark.parametrize("cmd", [
    C4_LS,
    "cat ~/.claude/plugins/x/SKILL.md | head -40",
    "grep -rn foo ~/.claude/plugins/cache",
    "find ~/.claude/plugins -name '*.md'",
    f"ls {S} 2>/dev/null | wc -l",
    f"sed -n 1,40p {S}/brainstorming/SKILL.md",
    f"test -d {S} && echo yes",
    f"grep -rn 'def main' {S} | head -20",
    f"find {S} -maxdepth 2 -type f -name 'SKILL.md'",
])
def test_plugins_reads_pass(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD) is None, cmd


@pytest.mark.parametrize("cmd", [
    "echo x > ~/.claude/plugins/a",
    "cat a >> ~/.claude/plugins/b",
    "echo x >| ~/.claude/plugins/a",
    "ls ~/.claude/plugins &> out.txt",
    "tee ~/.claude/plugins/a",
    "cp x ~/.claude/plugins/",
    "mv ~/.claude/plugins/a b",
    "rm -rf ~/.claude/plugins/x",
    "sed -i s/a/b/ ~/.claude/plugins/x",
    "sed --in-place s/a/b/ ~/.claude/plugins/x",
    "sed -ni p ~/.claude/plugins/x",
    "sed 's/a/b/w /tmp/out' ~/.claude/plugins/x",
    "sed -f evil.sed ~/.claude/plugins/x",
    "find ~/.claude/plugins -delete",
    "find ~/.claude/plugins -exec rm {} \\;",
    "find ~/.claude/plugins -execdir rm {} +",
    "find ~/.claude/plugins -fprint /tmp/x",
    "ls ~/.claude/plugins | xargs rm",
    "bash -c 'ls ~/.claude/plugins'",
    "python3 ~/.claude/plugins/x.py",
    "awk '{system(\"rm x\")}' ~/.claude/plugins/x",
    "awk '{print > \"/tmp/x\"}' ~/.claude/plugins/x",
    "awk -i inplace '{print}' ~/.claude/plugins/x",
    "sort -o ~/.claude/plugins/x y",
    "uniq a ~/.claude/plugins/out",
    "rg --pre ./evil foo ~/.claude/plugins",
    "ls ~/.claude/plugins ~/.claude/settings.json",
    "ls ~/.claude/plugins; git push",
    "ls ~/.claude/plugins && launchctl kickstart x",
    "cat ~/.claude/hooks/x",
    "ls ~/.claude/plugins; cat logs/conductor/cards.json",
    "ls ~/.claude/plugins\nrm -rf ~/x",
    "ls ~/.claude/plugins `rm -rf ~/x`",
    "ls ~/.claude/plugins $(rm -rf ~/x)",
    "(cd ~/.claude/plugins && rm x)",
    "cat ~/.claude/plugins/../hooks/x",
    "PATH=/tmp/evil ls ~/.claude/plugins",
    "ls ~/.claude/plugins & rm -rf ~/x",
    "ls ~/.claude/plugins || chmod 777 ~/.claude/plugins",
    "ls ~/.claude/plugins; git -c core.pager=evil log",
    "ls ~/.claude/plugins; git checkout x",
    "ls ~/.claude/plugins; git diff --output=/tmp/x",
    "ls ~/.claude/plugins; git worktree add x",
    "ls ~/.claude/plugins; git branch newbranch",
    "uniq ~/.claude/plugins/a 2",       # a bare '2' is uniq's output file, not a redirect
    "sort -o 1 ~/.claude/plugins/a",
    "uniq ~/.claude/plugins/a 2 >/dev/null",  # '2' then a space: stdout redirect, '2' is the output file
    "ls ~/.claude/plugins x2>/dev/null",
    # cold audit 2026-11-02 bypasses (P = a plugin hook script Claude Code runs later)
    "sort -uo ~/.claude/plugins/m/hooks/run.sh /tmp/payload",
    "sort --out=~/.claude/plugins/m/hooks/run.sh /tmp/payload",
    "sort --compress-program=/tmp/evil -S 1K ~/.claude/plugins/m/hooks/run.sh",
    "cat /tmp/payload | uniq - ~/.claude/plugins/m/hooks/run.sh",
    "O=-o; sort $O ~/.claude/plugins/m/hooks/run.sh /tmp/payload",
    "X=-delete; find ~/.claude/plugins/m $X",
    "find ~/.claude/plugins -name x $'\\x2dexec' /tmp/evil {} +",
    "sed $'\\x2di.bak' s/a/b/ ~/.claude/plugins/m/hooks/run.sh",
    "find ~/.claude/plugins/m -dele{t,t}e",
    "sed -e'1w ~/.claude/plugins/m/hooks/run.sh' /tmp/payload",
    "sed -n $'1w ~/.claude/plugins/m/hooks/run.sh' x",
    "sed 's/a/b/w~/.claude/plugins/m/hooks/run.sh' x",
    "sed -n --expression='1w /tmp/x' ~/.claude/plugins/m/x",
    "/tmp/evil/cat ~/.claude/plugins/m/x",
    "./ls ~/.claude/plugins",
    "awk -f /tmp/e.awk ~/.claude/plugins/m/x",
    "rg --hostname-bin=/tmp/evil --hyperlink-format=default x ~/.claude/plugins",
    "cd ~/.claude/plugins/m && file -C -m hooks",
    "ls ~/.claude/plugins/*",
    "cat \"~/.claude/plugins/$X\"",
    "ls ~/.claude/plugins; git -C /tmp/evilrepo status",
    "ls ~/.claude/plugins; git --git-dir=/tmp/x log",
    "find ~/.claude/plugins -newer x -fls /tmp/out",
    "S=-delete; find ~/.claude/plugins $S",          # a variable may never carry an option
    "ls ~/.claude/plugins $UNSET",                    # never-set variable: could be anything
    "S=/tmp; ls ~/.claude/plugins ${S}",
    "S=x ls ~/.claude/plugins",                       # VAR=x prog
    "ls ~/.claude/plugins; S=/tmp/evil; $S/cat x",    # program from a variable
    "cd /tmp/evilrepo && git log -p; ls ~/.claude/plugins",  # another repo's config can run programs
    # terminal review 1 (2026-11-02): bash glues adjacent quoted/unquoted words into ONE argument
    "sed -n '1p'';w ~/.claude/plugins/pwned' some.txt",
    "ca't' ~/.claude/plugins/x",
    "c'at' ~/.claude/plugins/x",
    "ls ~/.claude/plugins; git log '--out''put=x'",
    "ls 'a''b' ~/.claude/plugins",
    "ls ~/.claude/plugins/'x'y",
    # cwd persists between Bash calls: an allowed cd would aim the next, unchecked command there
    "cd ~/.claude/plugins/x",
    "cd ~/.claude/plugins && ls",
])
def test_plugins_writes_and_others_stay_gated(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD), cmd


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_plugins_file_writes_still_gated(tool):
    assert gated_reason(tool, {"file_path": str(HOME / ".claude/plugins/x")}, CWD)


NAV_LOGS = "/Users/Manoj/calwbot/logs/navigator"


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"),
                                      ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")])
@pytest.mark.parametrize("name", ["repos.json", "repos.json.bak", "repos.json.20261108", "credits.json"])
def test_navigator_control_files_write_gated(tool, key, name):
    assert gated_reason(tool, {key: f"{NAV_LOGS}/{name}"}, CWD)


@pytest.mark.parametrize("cmd", [
    f"cp x {NAV_LOGS}/repos.json", f"echo '[]' > {NAV_LOGS}/repos.json.bak",
    f"mv y {NAV_LOGS}/credits.json", "cd /Users/Manoj/calwbot/logs/navigator && cp x repos.json",
])
def test_navigator_control_files_bash_gated(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD)


@pytest.mark.parametrize("tool,key", [("Read", "file_path"), ("Grep", "path"), ("Glob", "path")])
def test_navigator_control_files_read_allowed(tool, key):
    assert gated_reason(tool, {key: f"{NAV_LOGS}/repos.json"}, CWD) is None
    assert gated_reason(tool, {key: f"{NAV_LOGS}/repos.json.bak"}, CWD) is None


def test_other_navigator_logs_not_gated():
    assert gated_reason("Edit", {"file_path": f"{NAV_LOGS}/foo.md"}, CWD) is None
    assert gated_reason("Write", {"file_path": f"{NAV_LOGS}/runs/x.json"}, CWD) is None
    assert gated_reason("Bash", {"command": f"echo hi > {NAV_LOGS}/foo.md"}, CWD) is None


DOC = "http://127.0.0.1:8090/api/v1/agent/docs/navigator/settings"


@pytest.mark.parametrize("cmd", [
    f"curl -X PUT {DOC} -d '{{\"mode\":\"execute\"}}'",
    f"curl -s -X POST {DOC} -H 'content-type: application/json' --data '{{}}'",
    "curl -X PATCH http://127.0.0.1:8090/api/v1/agent/docs/navigator%2Fsettings -d x",
    f"curl --request PUT {DOC} --json '{{}}'",
    f"curl {DOC} -d '{{\"mode\":\"execute\"}}'",
    f"python3 -c \"import requests; requests.put('{DOC}', json={{}})\"",
    f"python3 -c \"import httpx; httpx.post('{DOC}', json={{}})\"",
    "navigator settings set mode execute",
    "navigator settings unset mode",
    "/opt/homebrew/bin/navigator settings set merge_tier_b_batch true",
])
def test_navigator_settings_doc_write_gated(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD)


@pytest.mark.parametrize("cmd", [
    f"curl -s {DOC}",
    f"curl -X GET {DOC}",
    "navigator settings",
    "echo the navigator/settings document holds the mode",
    "grep -rn navigator/settings navigator/ | head",
    "curl -X POST http://127.0.0.1:8090/api/v1/agent/docs/other/thing -d x",
])
def test_navigator_settings_doc_read_or_mention_not_gated(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD) is None


# ---- restart-and-gates: specific gh rules, clone/fetch, GitHub API writes, pip/uv installs ----
from navigator.conductor import gate as _gate  # noqa: E402

GH_RULES = [
    ("gh repo edit o/r --visibility public", "visibility"),
    ("gh repo edit o/r --visibility private --accept-visibility-change-consequences", "visibility"),
    ("gh repo create x --public", "gh repo create"),
    ("gh repo delete o/r --yes", "gh repo delete"),
    ("gh api -X PATCH repos/o/r -f private=false", "gh api"),
    ("gh api repos/o/r --method POST -f name=x", "gh api"),
    ("gh api repos/o/r -F visibility=public", "gh api"),
    ("gh api -XPATCH repos/o/r -f private=false", "gh api"),
    ("gh api --method=DELETE repos/o/r", "gh api"),
    ("/opt/homebrew/bin/gh repo edit o/r --visibility public", "visibility"),
    ("bash -lc 'gh repo create x'", "gh repo create"),
    ("cd x && gh repo delete o/r", "gh repo delete"),
    ("gh -R o/r repo edit --visibility public", "visibility"),
]


@pytest.mark.parametrize("cmd,word", GH_RULES)
def test_gh_specific_reason(cmd, word):
    assert word in gated_reason("Bash", {"command": cmd}, CWD)


@pytest.mark.parametrize("cmd,word", GH_RULES)
def test_gh_reason_direct_bypasses_blanket(cmd, word):
    toks = [t for t in _gate._SPLIT.split(cmd.replace('"', " ").replace("'", " ")) if t]
    i = next(k for k, t in enumerate(toks) if t.rsplit("/", 1)[-1] == "gh")
    assert word in _gate._gh_reason(toks, i)


@pytest.mark.parametrize("cmd", ["gh pr view 1", "gh repo view o/r", "gh api repos/o/r", "gh issue list"])
def test_gh_reason_direct_does_not_stop_reads(cmd):
    assert _gate._gh_reason(cmd.split(), 0) is None


def test_github_api_curl_writes():
    for cmd in ["curl -X PATCH -H 'Authorization: token t' https://api.github.com/repos/o/r -d '{\"private\":false}'",
                "curl -X DELETE https://api.github.com/repos/o/r",
                "curl --request DELETE https://api.github.com/repos/o/r",
                "curl --request=DELETE https://api.github.com/repos/o/r",
                "python3 -c \"import requests;requests.delete('https://api.github.com/repos/o/r')\"",
                "curl -d x https://api.github.com/user/repos"]:
        assert "GitHub API" in gated_reason("Bash", {"command": cmd}, CWD), cmd
    assert gated_reason("Bash", {"command": "curl https://api.github.com/repos/o/r"}, CWD) is None


def test_github_api_regex_is_fast():
    import time
    t = time.time()
    gated_reason("Bash", {"command": "curl https://api.github.com/x " + "-X " * 20000}, CWD)
    gated_reason("Bash", {"command": "curl https://api.github.com/x " + "A" * 200000}, CWD)
    assert time.time() - t < 2


@pytest.mark.parametrize("cmd", [
    "git clone https://github.com/o/r", "git clone git@github.com:o/r.git /tmp/x", "git clone o/r",
    "git -C /tmp clone ssh://h/r", "git fetch", "git fetch origin", "git fetch https://x/y.git main",
    "git ls-remote https://x/y", "git submodule update --init", "git remote add u https://x/y",
    "git remote show origin", "git remote update", "git remote prune origin", "git remote set-url o x",
    "git clone host:path", "git config url.x.insteadOf y",
])
def test_git_network_gated(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD), cmd


@pytest.mark.parametrize("cmd", [
    "git clone /Users/Manoj/calwbot/navigator /tmp/w", "git clone ./a ../b",
    "git fetch /Users/Manoj/calwbot/navigator main", "git status", "git remote -v", "git remote get-url origin",
])
def test_git_local_not_gated(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD) is None, cmd


@pytest.mark.parametrize("cmd", [
    "pip install requests", "pip3 install requests", "python3 -m pip install x", "uv pip install x", "uv add x",
    "pip install git+https://x/y", ".venv/bin/pip install git+https://x/y",
    "/Users/Manoj/calwbot/navigator/.venv/bin/pip install requests",
    "uv sync", "uv run --with x y", "uv tool install x", "pipx install x", "uvx x", "cd a && pip install x",
])
def test_pip_installs_gated(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD), cmd


@pytest.mark.parametrize("cmd", ["pip list", "pip show x", "pip download x", "uv --version", "python3 -m pytest"])
def test_pip_reads_not_gated(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD) is None, cmd


def test_later_piece_cannot_supply_the_local_path():
    assert gated_reason("Bash", {"command": "git fetch; /bin/ls"}, CWD)
    assert gated_reason("Bash", {"command": "git clone && ./x"}, CWD)
    assert gated_reason("Bash", {"command": "git clone --depth 1 https://x/y ./z"}, CWD)
    assert gated_reason("Bash", {"command": "git clone --depth 1 ./a ./b"}, CWD) is None


def _bash_ms(cmd: str) -> float:
    import time
    from navigator.conductor.gate import _bash_reason
    best = 1e9
    for _ in range(3):
        t = time.perf_counter()
        _bash_reason(cmd)
        best = min(best, time.perf_counter() - t)
    return best * 1000


@pytest.mark.parametrize("unit,size", [
    ("x ", 50_000), ("pip install x ", 48_000), ("uv pip install x ", 36_000),
    ("curl https://api.github.com/x ", 48_000), ("pipx install x ", 48_000),
    ("git status ", 48_000),
])
def test_bash_reason_linear_time(unit, size):
    cmd = unit * (size // len(unit))
    baseline = _bash_ms("x" * 50_000)  # one huge token: same size, no per-token work
    took = _bash_ms(cmd)
    assert took < 100 and took < max(20 * baseline, 30), f"{took:.0f} ms vs baseline {baseline:.0f} ms"
