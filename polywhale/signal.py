"""CopySignal — the structured event the live watcher emits."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class CopySignal:
    """One observed trade from a watched whale, packaged as a copyable hint.

    The signal is fully descriptive and contains no order intent. A separate
    executor decides whether to act, at what size, on which venue.
    """

    source_wallet: str
    source_pseudonym: Optional[str]
    market_slug: str
    market_question: str
    condition_id: str
    asset_token_id: str
    outcome: str
    side: str
    price: float
    size: float
    usdc_size: float
    timestamp: int
    transaction_hash: Optional[str]
    style_hint: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
