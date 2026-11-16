"""Resolution policy: a binding exists, the target account exists, and it is enabled."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import Unresolvable
from .store import CurrentDirectory


@dataclass(frozen=True)
class Resolution:
    account_id: str
    enabled: bool
    revision: int

    def as_dict(self) -> dict[str, object]:
        return {"account_id": self.account_id, "enabled": self.enabled, "revision": self.revision}


def apply_policy(directory: CurrentDirectory, issuer: str, subject: str, account_id: str | None) -> Resolution:
    if account_id is None:
        raise Unresolvable(
            "external identity is not bound in the published directory",
            details={"revision": directory.revision},
        )
    account = directory.accounts.get(account_id)
    if account is None:  # pragma: no cover - snapshot validation makes this unreachable
        raise Unresolvable(
            "bound account is not present in the published directory",
            details={"revision": directory.revision},
        )
    if not account.enabled:
        raise Unresolvable(
            "bound account is disabled",
            details={"revision": directory.revision, "account_id": account_id, "enabled": False},
        )
    return Resolution(account_id=account.account_id, enabled=True, revision=directory.revision)
