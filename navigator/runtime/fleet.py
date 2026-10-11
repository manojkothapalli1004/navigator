from navigator.observability.notify import NullNotifier


class Fleet:
    """Several single-repo Navigators run as one daemon. Planning rotates one repo per
    cycle (one planner call per cycle, not one per repo); execution and merge decisions
    are processed for every repo every cycle."""

    def __init__(self, navigators: list):
        if not navigators:
            raise ValueError("Fleet needs at least one repo")
        self.navigators = navigators
        self._next = 0
        self.last_snapshot_chars = 0
        self.notifier = NullNotifier()

    @property
    def driver(self):
        return self.navigators[0].driver

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == "notifier" and "navigators" in self.__dict__:
            for nav in self.navigators:
                nav.notifier = value

    def periodic_tick(self) -> list:
        nav = self.navigators[self._next % len(self.navigators)]
        self._next += 1
        created = nav.periodic_tick()
        self.last_snapshot_chars = nav.last_snapshot_chars
        return created

    def approval_poll(self) -> list:
        return [run for nav in self.navigators for run in nav.approval_poll()]

    def merge_poll(self) -> list:
        return [run for nav in self.navigators for run in nav.merge_poll()]

    def reconcile_on_startup(self) -> None:
        # runs live in the shared ACT; one reconcile covers every repo
        self.navigators[0].reconcile_on_startup()
