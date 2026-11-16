"""Typed errors mapped onto HTTP status codes by the service layer."""

from __future__ import annotations


class ServiceError(Exception):
    """Base class. ``status`` is the HTTP status the API layer returns."""

    status = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class InvalidRequest(ServiceError):
    status = 400
    code = "invalid_request"


class ValidationError(ServiceError):
    """The JSON body / snapshot violates the documented schema."""

    status = 422
    code = "invalid_snapshot"

    def __init__(self, message: str, *, path: str = "", index: object = None) -> None:
        super().__init__(message)
        self.path = path
        self.index = index


class InvalidCredentials(ServiceError):
    status = 401
    code = "invalid_adapter_credentials"


class NotFound(ServiceError):
    status = 404
    code = "not_found"


class Unresolvable(ServiceError):
    """The identity is not bound, or its account is disabled."""

    status = 403
    code = "identity_not_resolvable"


class StaleRevision(ServiceError):
    status = 409
    code = "stale_revision"


class RevisionConflict(ServiceError):
    status = 409
    code = "revision_conflict"


class RevisionTaken(ServiceError):
    """A database constraint rejected the write (defence in depth).

    Caller-side validation should make this unreachable; the status is still a
    client-visible 409 because it is a conflict with existing state, not a bug
    in the caller's request shape.
    """

    status = 409
    code = "revision_conflict"


class StoreBusy(ServiceError):
    status = 503
    code = "store_busy"


