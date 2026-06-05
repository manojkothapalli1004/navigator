from typing import Protocol
import httpx

class ORCClient(Protocol):
    def get_canonical_memory(self) -> dict: ...
    def get_digest(self) -> dict: ...
    def apply_memory_update(self, section: str, note: str, replace: bool = False) -> None: ...

class FakeORCClient:
    def __init__(self, memory: dict | None = None):
        self._memory = memory or {}
        self.updates: list[tuple[str, str, bool]] = []
    def get_canonical_memory(self) -> dict:
        return dict(self._memory)
    def get_digest(self) -> dict:
        return {"digest": "fake"}
    def apply_memory_update(self, section: str, note: str, replace: bool = False) -> None:
        self.updates.append((section, note, replace))

class HTTPORCClient:
    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client()
    def get_canonical_memory(self) -> dict:
        return self.client.get(f"{self.base_url}/api/memory").json()
    def get_digest(self) -> dict:
        return self.client.get(f"{self.base_url}/api/memory/digest").json()
    def apply_memory_update(self, section: str, note: str, replace: bool = False) -> None:
        self.client.post(f"{self.base_url}/api/memory/apply-update",
                         json={"section": section, "note": note, "replace": replace,
                               "source": "navigator"}).raise_for_status()
