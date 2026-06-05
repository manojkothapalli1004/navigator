from navigator.runtime.drivers.base import ROLE_PERMISSIONS
from navigator.contracts.enums import Role

def test_role_permissions():
    assert ROLE_PERMISSIONS[Role.planner] == {"read"}
    assert ROLE_PERMISSIONS[Role.builder] == {"read","write","run"}
    assert ROLE_PERMISSIONS[Role.verifier] == {"read","run"}
    assert "write" not in ROLE_PERMISSIONS[Role.verifier]
