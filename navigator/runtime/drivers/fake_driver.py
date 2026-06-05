from navigator.runtime.drivers.base import DriverRequest, DriverResult
from navigator.contracts.enums import Role

class FakeSubagentDriver:
    def run_role(self, request: DriverRequest) -> DriverResult:
        if request.role == Role.planner:
            repo = request.context.get("repo", "demo")
            return DriverResult(role=Role.planner, proposals=[{
                "repo": repo, "title": "fake: touch file",
                "rationale": "deterministic fake plan",
                "intended_changes": "add navigator_touch.py",
                "affected_files": ["navigator_touch.py"], "risk_tier": "low",
            }], summary="planned 1 change")
        if request.role == Role.builder:
            return DriverResult(role=Role.builder,
                                file_writes=[("navigator_touch.py", "# navigator v0 fake change\n")],
                                summary="wrote 1 file")
        return DriverResult(role=Role.verifier,
                            commands=[["python3","-m","py_compile","navigator_touch.py"]],
                            summary="verify via py_compile")
