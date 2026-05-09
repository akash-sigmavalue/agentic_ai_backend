from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PolicyDecision(BaseModel):
    """Decision returned by the policy layer before Gmail connector execution."""

    action: Literal["send", "draft", "skip", "approval_required"] = "draft"
    reason: str = "Default to draft."
    confidence: float = 0.0
    requires_approval: bool = False


class PolicyAgent:
    """Simple decision layer that chooses whether Gmail output may be sent."""

    def decide(
        self,
        *,
        email_content: Any,
        sender: str | None,
        rule_config: dict[str, Any] | None = None,
        confidence_score: float | None = None,
    ) -> PolicyDecision:
        config = dict(rule_config or {})
        confidence = self._normalize_confidence(confidence_score if confidence_score is not None else config.get("confidence"))
        sender_email = (sender or "").strip().lower()
        auto_send_allowed = bool(config.get("auto_send_allowed") or config.get("send_directly"))
        trust_level = self._normalize_confidence(config.get("trust_level"))
        require_approval = bool(config.get("require_approval", True))

        if config.get("skip") or config.get("disabled"):
            return PolicyDecision(action="skip", reason="Policy marked the email as skipped.", confidence=confidence)

        if sender_email and auto_send_allowed and trust_level >= 0.75 and confidence >= 0.8:
            return PolicyDecision(
                action="send",
                reason="Trusted sender with high confidence, auto send approved.",
                confidence=confidence,
                requires_approval=False,
            )

        if confidence >= 0.5:
            return PolicyDecision(
                action="draft",
                reason="Confidence is moderate, so the reply should be drafted first.",
                confidence=confidence,
                requires_approval=False,
            )

        if require_approval:
            return PolicyDecision(
                action="approval_required",
                reason="Sender is not trusted enough for auto send.",
                confidence=confidence,
                requires_approval=True,
            )

        return PolicyDecision(action="skip", reason="Policy rejected auto send.", confidence=confidence)

    def _normalize_confidence(self, value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        if confidence < 0.0:
            return 0.0
        if confidence > 1.0:
            return 1.0
        return confidence
