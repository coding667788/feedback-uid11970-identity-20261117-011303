"""Pre-provisioned local test adapters and their opaque credentials.

Each adapter represents one upstream identity provider for one tenant. Only
the credential token authenticates a resolve call; the tenant recorded for the
adapter is authoritative, so a caller can never resolve into another tenant by
supplying a tenant name in the request body.

Credential material lives in a separate file from the directory database so a
directory dump never contains a token, and the HTTP responses, test logs and
CLI output never echo the token.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from dataclasses import dataclass
from typing import Any

from .errors import InvalidCredentials

TOKEN_PREFIX = "adapter_"

_ADAPTER_SCHEMA = {"adapters": list}


@dataclass(frozen=True)
class Adapter:
    adapter_id: str
    tenant: str
    issuer: str
    credential: str

    def public(self) -> dict[str, str]:
        """Adapter metadata safe to display (never includes the credential)."""
        return {"adapter_id": self.adapter_id, "tenant": self.tenant, "issuer": self.issuer}


class AdapterRegistry:
    def __init__(self, adapters: dict[str, Adapter]) -> None:
        self._adapters = adapters

    def __len__(self) -> int:
        return len(self._adapters)

    @classmethod
    def load(cls, path: str) -> "AdapterRegistry":
        with open(path, "rb") as handle:
            raw = json.loads(handle.read().decode("utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"adapters"}:
            raise ValueError("adapter file must be an object with exactly one key 'adapters'")
        entries = raw["adapters"]
        if not isinstance(entries, list):
            raise ValueError("'adapters' must be an array")
        adapters: dict[str, Adapter] = {}
        ids: set[str] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {"adapter_id", "tenant", "issuer", "credential"}:
                raise ValueError(
                    f"adapters[{index}] must contain exactly adapter_id, tenant, issuer, credential"
                )
            for field in ("adapter_id", "tenant", "issuer", "credential"):
                if not isinstance(entry[field], str) or entry[field] == "":
                    raise ValueError(f"adapters[{index}].{field} must be a non-empty string")
            credential = entry["credential"]
            if credential in adapters:
                raise ValueError("duplicate adapter credential in adapter file")
            if entry["adapter_id"] in ids:
                raise ValueError(f"duplicate adapter_id {entry['adapter_id']!r}")
            ids.add(entry["adapter_id"])
            adapters[credential] = Adapter(
                adapter_id=entry["adapter_id"],
                tenant=entry["tenant"],
                issuer=entry["issuer"],
                credential=credential,
            )
        return cls(adapters)

    def authenticate(self, token: str | None) -> Adapter:
        if not isinstance(token, str) or token == "":
            raise InvalidCredentials("missing adapter credential")
        # Constant-time comparison against every known credential; the loop is
        # bounded by the number of pre-provisioned adapters.
        matched: Adapter | None = None
        for known, adapter in self._adapters.items():
            if hmac.compare_digest(known, token):
                matched = adapter
        if matched is None:
            raise InvalidCredentials("adapter credential not recognised")
        return matched

    def by_id(self, adapter_id: str) -> Adapter | None:
        for adapter in self._adapters.values():
            if adapter.adapter_id == adapter_id:
                return adapter
        return None

    def public_list(self) -> list[dict[str, str]]:
        return [a.public() for a in sorted(self._adapters.values(), key=lambda a: a.adapter_id)]


def generate_adapter_file(path: str, specs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Create an adapter file with fresh random credentials.

    ``specs`` entries carry ``adapter_id``, ``tenant`` and ``issuer``. Returns
    the public metadata only; the caller decides how (or whether) to surface
    the credentials to a human, e.g. for a manual example run.
    """
    adapters = []
    for spec in specs:
        adapters.append(
            {
                "adapter_id": spec["adapter_id"],
                "tenant": spec["tenant"],
                "issuer": spec["issuer"],
                "credential": TOKEN_PREFIX + secrets.token_urlsafe(24),
            }
        )
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"adapters": adapters}, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows ACLs differ
        pass
    return [Adapter(a["adapter_id"], a["tenant"], a["issuer"], a["credential"]).public() for a in adapters]
