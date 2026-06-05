from enum import Enum

class ProposalStatus(str, Enum):
    proposed = "proposed"; approved = "approved"; declined = "declined"
    expired = "expired"; superseded = "superseded"

class RunStatus(str, Enum):
    executing = "executing"; completed = "completed"; failed = "failed"
    rolled_back = "rolled_back"; aborted = "aborted"

class RunPhase(str, Enum):
    pending = "pending"; planning = "planning"; building = "building"
    verifying = "verifying"; awaiting_review = "awaiting_review"
    merging = "merging"; finalizing = "finalizing"

class ApprovalGate(str, Enum):
    review = "review"; merge = "merge"; architecture = "architecture"
    destructive = "destructive"; budget = "budget"; deploy = "deploy"
    @classmethod
    def implemented_v0(cls) -> set["ApprovalGate"]:
        return {cls.review, cls.merge}

class ApprovalDecision(str, Enum):
    pending = "pending"; approved = "approved"; declined = "declined"

class RiskTier(str, Enum):
    low = "low"; medium = "medium"; high = "high"; destructive = "destructive"

class Role(str, Enum):
    planner = "planner"; builder = "builder"; verifier = "verifier"

class Severity(str, Enum):
    info = "info"; warning = "warning"; error = "error"; critical = "critical"
