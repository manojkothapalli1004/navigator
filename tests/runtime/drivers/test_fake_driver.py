from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.runtime.drivers.base import DriverRequest
from navigator.contracts.enums import Role

def test_fake_driver_roles_are_deterministic():
    d = FakeSubagentDriver()
    plan = d.run_role(DriverRequest(role=Role.planner, context={"repo":"trader"}))
    assert len(plan.proposals) == 1 and plan.proposals[0]["repo"] == "trader"
    build = d.run_role(DriverRequest(role=Role.builder, context={}, worktree_path="/tmp/x"))
    assert build.file_writes == [("navigator_touch.py", "# navigator v0 fake change\n")]
    verify = d.run_role(DriverRequest(role=Role.verifier, context={}, worktree_path="/tmp/x"))
    assert verify.commands == [["python","-m","py_compile","navigator_touch.py"]]
