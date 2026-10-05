from dataclasses import dataclass, field
from typing import List, Optional, Set


class ApiError(Exception):
    """Error carrying an HTTP status and a spec error code."""

    def __init__(self, status_code: int, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.status_code = status_code
        self.code = code
        self.message = message or code


@dataclass
class User:
    id: str
    email: str
    password_hash: str
    display_name: str
    handle: str
    balance: int = 0
    tokens: Set[str] = field(default_factory=set)


@dataclass
class Payment:
    id: str
    from_user_id: str
    to_user_id: str
    amount: int
    currency: str
    note: str
    visibility: str
    request_id: Optional[str]
    settlement_id: Optional[str]
    created_at: str


@dataclass
class Request:
    id: str
    requester_id: str
    payer_id: str
    amount: int
    currency: str
    note: str
    status: str
    payment_id: Optional[str]
    created_at: str


@dataclass
class Settlement:
    id: str
    committed_at: str
    payment_ids: List[str]


@dataclass
class IdempotencyRecord:
    user_id: str
    key: str
    method: str
    path: str
    body: object
    response: object
    status: int
