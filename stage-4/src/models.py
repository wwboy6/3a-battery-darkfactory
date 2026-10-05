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
    opening_balance: int = 0
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
    authorization_id: Optional[str] = None
    refund_of: Optional[str] = None


@dataclass
class Revision:
    revision: int
    amount: int
    effective_at: str
    recorded_at: str
    reason: str = ""
    correction_batch_id: Optional[str] = None


@dataclass
class CorrectionBatch:
    id: str
    recorded_at: str
    members: List[dict] = field(default_factory=list)


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
class Authorization:
    id: str
    from_user_id: str
    to_user_id: str
    amount: int
    captured_amount: int
    currency: str
    note: str
    visibility: str
    status: str
    expires_at: str
    payment_id: Optional[str]
    payment_ids: List[str]
    created_at: str
    closed_at: Optional[str] = None


@dataclass
class IdempotencyRecord:
    user_id: str
    key: str
    method: str
    path: str
    body: object
    response: object
    status: int
