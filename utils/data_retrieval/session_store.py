from dataclasses import dataclass, field
from threading import Lock
from typing import Optional
from uuid import uuid4


@dataclass
class ClarificationTurn:
    questions: list[str]
    answer: Optional[str] = None


@dataclass
class ChatSession:
    session_id: str
    history: list[dict] = field(default_factory=list)
    pending_clarification: Optional[dict] = None


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, ChatSession] = {}
        self._lock = Lock()

    def get_or_create(self, session_id: Optional[str] = None) -> ChatSession:
        with self._lock:
            resolved_id = session_id or str(uuid4())
            if resolved_id not in self._sessions:
                self._sessions[resolved_id] = ChatSession(session_id=resolved_id)
            return self._sessions[resolved_id]

    def add_message(self, session: ChatSession, role: str, content: str) -> None:
        session.history.append({"role": role, "content": content})

    def has_pending_clarification(self, session: ChatSession) -> bool:
        return bool(session.pending_clarification)

    def build_effective_question(self, session: ChatSession, latest_user_message: str) -> str:
        pending = session.pending_clarification
        if not pending:
            return latest_user_message

        turns = pending.get("turns") or []
        if turns and turns[-1].answer is None:
            turns[-1].answer = latest_user_message

        blocks = []
        for index, turn in enumerate(turns, start=1):
            question_block = "\n".join(f"- {question}" for question in turn.questions)
            answer_block = turn.answer or ""
            blocks.append(
                f"Clarification round {index} questions:\n{question_block}\nUser answer:\n{answer_block}"
            )
        clarification_context = "\n\n".join(blocks)

        return (
            "Resolve the real-estate intent using the original request and the clarification replies below.\n\n"
            f"Original request:\n{pending.get('base_question', '')}\n\n"
            f"{clarification_context}"
        )

    def set_pending_clarification(
        self,
        session: ChatSession,
        base_question: str,
        reason: str,
        questions: list[str],
    ) -> None:
        existing = session.pending_clarification
        if existing and existing.get("turns"):
            turns = list(existing["turns"])
            if turns[-1].answer is not None:
                turns.append(ClarificationTurn(questions=questions))
            else:
                turns[-1] = ClarificationTurn(questions=questions)
            session.pending_clarification = {
                "base_question": existing.get("base_question", base_question),
                "reason": reason,
                "turns": turns,
            }
            return

        session.pending_clarification = {
            "base_question": base_question,
            "reason": reason,
            "turns": [ClarificationTurn(questions=questions)],
        }

    def clear_pending_clarification(self, session: ChatSession) -> None:
        session.pending_clarification = None
