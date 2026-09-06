import json
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

VoteChoice = Literal["approve", "reject", "abstain"]


class MagiVote(BaseModel):
    vote: VoteChoice
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reasoning: str
    concerns: List[str] = Field(default_factory=list)


class MindVerdict(BaseModel):
    mind_name: str
    vote: VoteChoice
    confidence: float
    reasoning: str
    concerns: List[str]
    model_used: str
    latency_ms: int


class DeliberationOutcome(BaseModel):
    action_id: str
    tool_name: str
    tier: int
    magi_enabled: bool
    verdicts: Dict[str, MindVerdict]  # "MELCHIOR", "BALTHASAR", "CASPER"
    approved_count: int
    rejected_count: int
    abstained_count: int
    consensus_type: str  # "UNANIMOUS_CONSENSUS", "MAJORITY_APPROVAL", "REJECTED", "BLOCKED_DISAGREEMENT", "BYPASSED_DISABLED"
    passed: bool
    requires_human_confirmation: bool
    explanation: str
