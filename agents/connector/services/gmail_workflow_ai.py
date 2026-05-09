from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.prompts import ChatPromptTemplate

# from api.schemas.connector.workflow_models import GmailFilters
from core.config import settings
from api.schemas.connector.workflow_models import (
    AutomationAction,
    AutomationIntent,
    AutomationTrigger,
    GmailFilters,
    GmailIntent,
    GmailOutputRequirement,
)


CONTACT_ALIASES: dict[str, str] = {}

LATEST_MAIL_REFERENCES: tuple[str, ...] = (
    "recently coming mail",
    "recently comming mail",
    "recent mail",
    "latest mail",
    "last email",
    "newest email",
    "recently received email",
)


def is_natural_language_latest_reference(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", (value or "").strip().lower())
    if not normalized:
        return False
    if normalized in LATEST_MAIL_REFERENCES:
        return True
    if any(term in normalized for term in ("recent", "latest", "newest", "last")) and any(
        term in normalized for term in ("mail", "email", "message")
    ):
        return True
    return False

NORMALIZATION_RULES: tuple[tuple[str, str], ...] = (
    (r"\bwhat\s+every\b", "whenever"),
    (r"\banalyse\b", "analyze"),
    (r"\bmail me\b", "sends me email"),
    (r"\bsend me mail\b", "sends me email"),
    (r"\bsend me email\b", "sends me email"),
    (r"\brespond them\b", "reply to them"),
    (r"\bresponse\b", "reply"),
    (r"\bpolitely\b", "polite"),
)

REPLY_PHRASES: tuple[str, ...] = (
    "reply",
    "respond",
    "response",
    "answer",
    "send reply",
    "auto reply",
    "reply them",
    "respond them",
)

DRAFT_PHRASES: tuple[str, ...] = (
    "draft reply",
    "draft",
    "prepare reply",
    "write reply",
)

SEND_PHRASES: tuple[str, ...] = (
    "send reply",
    "send them reply",
    "send reply to",
    "send it",
    "send directly",
    "auto reply",
)

AUTOMATION_PHRASES: tuple[str, ...] = (
    "if ",
    "whenever",
    "when i receive",
    "when someone sends",
    "every time",
    "automatically",
    "new email from",
    "new mails from",
    "new email arrives from",
    "new email arrives",
    "if someone sends",
)

TONE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bpolitely\b", "polite"),
    (r"\bpolite\b", "polite"),
    (r"\bprofessional\b", "professional"),
    (r"\bfriendly\b", "friendly"),
    (r"\bformal\b", "formal"),
)


class GmailWorkflowAI:
    """Shared Gmail planning and LLM helper for dynamic connector workflows."""

    def __init__(self) -> None:
        self._llm = self._build_llm()

    def extract_intent(self, prompt: str) -> GmailIntent | None:
        raw_prompt = (prompt or "").strip()
        normalized = self._normalize_prompt(raw_prompt)
        if not normalized:
            return None

        if not self._looks_gmail_related(normalized):
            return None

        is_automation = self._is_automation_prompt(normalized)
        sender_email, sender_name = self._extract_sender_details(raw_prompt, preserve_name=is_automation)
        operation = self._extract_operation(normalized, is_automation=is_automation)

        filters = self._extract_filters(normalized)
        print("DEBUG:", {
                    "normalized": normalized,
                    "is_automation": is_automation,
                    "operation": operation,
                    "filters": filters
                })
        if is_automation:
            filters.sender_name = sender_name
            filters.sender_email = sender_email
            filters.sender = sender_email or sender_name
            filters.is_unread = False
            filters.latest = False
            if not filters.keywords:
                filters.keywords = self._extract_keywords(normalized)

            output_requirement = self._extract_automation_output_requirement(normalized, operation)
            trigger_type = "new_email"
            return GmailIntent(
                execution_type="automation_rule",
                connector="gmail",
                trigger_type=trigger_type,
                operation=operation,
                filters=filters,
                output_requirement=output_requirement,
            )

        output_requirement = self._extract_output_requirement(normalized, operation)
        if operation in {"read", "summarize", "analyze", "report", "draft_reply", "send_reply"} and not filters.max_results:
            filters.max_results = 10
        if filters.latest:
            filters.max_results = 1

        return GmailIntent(
            execution_type="one_time_action",
            connector="gmail",
            operation=operation,
            filters=filters,
            output_requirement=output_requirement,
        )

    def _is_automation_prompt(self, prompt: str) -> bool:
        lower = prompt.lower()
        if self._contains_automation_clause(lower):
            return True
        if "new email from" in lower or "new emails from" in lower:
            return True
        if re.search(r"\b(?:if|when|whenever)\s+[\w\.\-@ ]+\s+sends?\s+(?:me\s+)?(?:mail|email|emails|mails)\b", lower):
            return True
        return False

    def _contains_automation_clause(self, lower_prompt: str) -> bool:
        return any(phrase in lower_prompt for phrase in AUTOMATION_PHRASES)

    def _extract_automation_output_requirement(self, prompt: str, operation: str) -> GmailOutputRequirement:
        lower = prompt.lower()
        summary = any(word in lower for word in ["summarize", "summarise", "summary"])
        analysis = any(word in lower for word in ["analyse", "analyze", "analysis", "report"])
        reply_required = any(word in lower for word in ["reply", "respond", "answer", "send"])
        draft_only = "draft" in lower and "send" not in lower
        send_directly = reply_required and not draft_only
        if operation == "draft_reply":
            draft_only = True
            send_directly = False
            reply_required = True
        if operation == "analyse_and_reply":
            analysis = True
            reply_required = True
        tone = self._extract_tone(lower)
        return GmailOutputRequirement(
            summary=summary,
            analysis=analysis,
            analytic_report="report" in lower,
            reply_required=reply_required,
            draft_only=draft_only,
            send_directly=send_directly,
            tone=tone,
        )

    def build_gmail_query(self, filters: GmailFilters) -> str:
        clauses: list[str] = []

        if filters.sender:
            clauses.append(f"from:{self._normalize_query_value(filters.sender)}")

        if filters.to:
            clauses.append(f"to:{self._normalize_query_value(filters.to)}")

        if filters.subject:
            clauses.append(f"subject:{self._normalize_query_value(filters.subject)}")

        if filters.is_unread:
            clauses.append("is:unread")

        if filters.has_attachment:
            clauses.append("has:attachment")

        if filters.date_range:
            clauses.extend(self._date_range_to_clauses(filters.date_range))

        for keyword in filters.keywords:
            cleaned = keyword.strip()
            if cleaned:
                clauses.append(self._normalize_query_value(cleaned))

        return " ".join(clauses).strip()

    def summarize(self, content: Any, instruction: str | None = None) -> dict[str, Any]:
        text = self._stringify(content)
        prompt = self._build_llm_prompt(
            system="Summarize the Gmail content clearly and concisely.",
            human=f"Instruction: {instruction or 'Provide a concise summary.'}\n\nContent:\n{text}",
        )
        summary = self._invoke_text_model(prompt, fallback=self._fallback_summary(text))
        return {"summary": summary, "source": content}

    def analyze(self, content: Any, instruction: str | None = None) -> dict[str, Any]:
        text = self._stringify(content)
        prompt = self._build_llm_prompt(
            system="Analyze the Gmail content and identify important emails, intent, and action items.",
            human=f"Instruction: {instruction or 'Analyze the messages.'}\n\nContent:\n{text}",
        )
        analysis = self._invoke_text_model(prompt, fallback=self._fallback_analysis(text))
        return {"analysis": analysis, "source": content}

    def generate_report(self, content: Any, instruction: str | None = None) -> dict[str, Any]:
        text = self._stringify(content)
        prompt = self._build_llm_prompt(
            system="Create a professional analytical report from the Gmail content.",
            human=f"Instruction: {instruction or 'Generate an analytical report.'}\n\nContent:\n{text}",
        )
        report = self._invoke_text_model(prompt, fallback=self._fallback_report(text))
        return {"report": report, "source": content}

    def generate_reply(self, content: Any, instruction: str | None = None, tone: str = "professional") -> dict[str, Any]:
        text = self._stringify(content)
        prompt = self._build_llm_prompt(
            system="Write a reply to the email thread using the requested tone and keeping it natural.",
            human=f"Tone: {tone}\nInstruction: {instruction or 'Draft a helpful reply.'}\n\nContext:\n{text}",
        )
        reply = self._invoke_text_model(prompt, fallback=self._fallback_reply(instruction or "", tone))
        return {"reply": reply, "source": content, "tone": tone}

    # def _extract_operation(self, prompt: str, *, is_automation: bool = False) -> str:
    #     lower = prompt.lower()

    #     draft_requested = any(phrase in lower for phrase in DRAFT_PHRASES)
    #     if draft_requested and not any(phrase in lower for phrase in SEND_PHRASES):
    #         return "draft_reply"

    #     send_requested = any(phrase in lower for phrase in SEND_PHRASES)
    #     reply_requested = any(phrase in lower for phrase in REPLY_PHRASES)

    #     if reply_requested or send_requested:
    #         return "analyse_and_reply" if is_automation else "send_reply"

    #     if any(word in lower for word in ["report", "analytic report", "analysis"]):
    #         return "report"

    #     if any(word in lower for word in ["analyze", "analyse", "important", "importance"]):
    #         return "analyze"

    #     if any(word in lower for word in ["summarize", "summary", "summarise"]):
    #         return "summarize"

    #     if any(word in lower for word in ["read", "show", "check", "find", "search"]):
    #         return "search"

    #     return "search"

    def _extract_operation(self, prompt: str, *, is_automation: bool = False) -> str:
        lower = prompt.lower()

        draft_requested = any(phrase in lower for phrase in DRAFT_PHRASES)
        if draft_requested and not any(phrase in lower for phrase in SEND_PHRASES):
          return "draft_reply"

        send_requested = any(phrase in lower for phrase in SEND_PHRASES)
        reply_requested = any(phrase in lower for phrase in REPLY_PHRASES)

        analysis_requested = any(
        word in lower
        for word in [
            "analyze",
            "analyse",
            "analysis",
            "analysing",
            "analyzing",
            "important",
            "importance",
        ]
    )

        if (reply_requested or send_requested) and analysis_requested:
          return "analyse_and_reply"

        if reply_requested or send_requested:
          return "analyse_and_reply" if is_automation else "send_reply"

        if any(word in lower for word in ["report", "analytic report"]):
          return "report"

        if analysis_requested:
          return "analyze"

        if any(word in lower for word in ["summarize", "summary", "summarise"]):
          return "summarize"

        if any(word in lower for word in ["read", "show", "check", "find", "search"]):
          return "search"

          return "search"








































    def _extract_output_requirement(self, prompt: str, operation: str) -> GmailOutputRequirement:
        lower = prompt.lower()
        send_directly = operation == "send_reply"
        draft_only = operation == "draft_reply"

        return GmailOutputRequirement(
            summary=operation == "summarize",
            analysis=operation == "analyze",
            analytic_report=operation == "report",
            reply_required=operation in {"draft_reply", "send_reply"},
            draft_only=draft_only,
            send_directly=send_directly,
            tone=self._extract_tone(lower),
        )

    def _extract_filters(self, prompt: str) -> GmailFilters:
        lower = prompt.lower()
        sender = self._extract_sender(prompt)
        to = self._extract_to(prompt)
        subject = self._extract_subject(prompt)
        keywords = self._extract_keywords(prompt)
        is_unread = "unread" in lower
        has_attachment = "attachment" in lower or "attachments" in lower
        latest = any(word in lower for word in ["latest", "most recent", "newest", "recent"])
        date_range = self._extract_date_range(lower)
        max_results = 1 if latest else 10
        if "all unread" in lower or "all mails" in lower or "all emails" in lower:
            max_results = 20

        return GmailFilters(
            sender=sender,
            to=to,
            subject=subject,
            keywords=keywords,
            is_unread=is_unread,
            has_attachment=has_attachment,
            date_range=date_range,
            latest=latest,
            max_results=max_results,
        )

    def _extract_sender(self, prompt: str) -> str | None:
        sender_email, sender_name = self._extract_sender_details(prompt, preserve_name=False)
        return sender_email or sender_name

    def _extract_sender_details(self, prompt: str, *, preserve_name: bool) -> tuple[str | None, str | None]:
        direct_email = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", prompt)
        if direct_email:
            email = direct_email.group(0).strip().rstrip(".,;:")
            return email, None

        patterns = [
            r"\bmail of\s+([^\.,;]+)",
            r"\bemail of\s+([^\.,;]+)",
            r"\bmails of\s+([^\.,;]+)",
            r"\bemails of\s+([^\.,;]+)",
            r"\bsent by\s+([^\.,;]+)",
            r"\b([^\.,;]+?)\s+sends?\s+(?:an?\s+)?(?:mail|email|emails|mails)\b",
            r"\bfrom\s+([^\.,;]+)",
            r"\bfrom:([^\s]+)",
            r"\bmail from\s+([^\.,;]+)",
            r"\bemails from\s+([^\.,;]+)",
        ]
        match = self._first_match(patterns, prompt)
        cleaned = self._clean_entity(match)
        if cleaned:
            alias = self._resolve_contact_alias(cleaned)
            if alias:
                return alias, cleaned if preserve_name else None
            return (None, cleaned) if preserve_name else (cleaned, None)

        alias = self._resolve_contact_alias(prompt)
        if alias:
            return alias, None if not preserve_name else self._best_guess_display_name(prompt)

        return None, self._best_guess_display_name(prompt) if preserve_name else None

    def _best_guess_display_name(self, prompt: str) -> str | None:
        patterns = [
            r"\b(?:if|when|whenever)\s+([A-Za-z][A-Za-z .'\-]{1,80}?)\s+sends?\s+(?:me\s+)?(?:mail|email|emails|mails)\b",
            r"\bfrom\s+([A-Za-z][A-Za-z .'\-]{1,80}?)\b",
        ]
        match = self._first_match(patterns, prompt)
        return self._clean_entity(match)

    def _extract_to(self, prompt: str) -> str | None:
        patterns = [
            r"\bto\s+([^\.,;]+)",
            r"\bto:([^\s]+)",
        ]
        match = self._first_match(patterns, prompt)
        if match and (is_natural_language_latest_reference(match) or self._looks_like_generic_mail_reference(match)):
            return None
        return self._clean_entity(match)

    def _extract_subject(self, prompt: str) -> str | None:
        patterns = [
            r"\bsubject\s+([^\.,;]+)",
            r"\babout\s+([^\.,;]+)",
            r"\bregarding\s+([^\.,;]+)",
        ]
        match = self._first_match(patterns, prompt)
        return self._clean_entity(match)

    def _extract_keywords(self, prompt: str) -> list[str]:
        lower = prompt.lower()
        phrases: list[str] = []

        for label in ["about", "regarding", "on", "for"]:
            match = re.search(rf"\b{label}\s+(.+?)(?:,| and | then | please | draft | reply | summarize | analyse| analyze| report|$)", lower, re.IGNORECASE)
            if match:
                phrase = match.group(1).strip()
                if phrase:
                    phrases.append(phrase)

        quoted = re.findall(r'"([^"]+)"', prompt)
        phrases.extend([item.strip() for item in quoted if item.strip()])

        seen: set[str] = set()
        keywords: list[str] = []
        for phrase in phrases:
            if phrase not in seen:
                seen.add(phrase)
                keywords.append(phrase)

        return keywords

    def _extract_date_range(self, lower_prompt: str) -> str | None:
        if "yesterday" in lower_prompt:
            return "yesterday"
        if "today" in lower_prompt:
            return "today"
        if "last week" in lower_prompt:
            return "last_week"
        if "last month" in lower_prompt:
            return "last_month"
        if "this week" in lower_prompt:
            return "this_week"
        return None

    def _date_range_to_clauses(self, date_range: str) -> list[str]:
        now = datetime.now(timezone.utc)
        if date_range == "today":
            start = now.strftime("%Y/%m/%d")
            end = (now + timedelta(days=1)).strftime("%Y/%m/%d")
            return [f"after:{start}", f"before:{end}"]
        if date_range == "yesterday":
            start = (now - timedelta(days=1)).strftime("%Y/%m/%d")
            end = now.strftime("%Y/%m/%d")
            return [f"after:{start}", f"before:{end}"]
        if date_range == "last_week":
            return ["newer_than:7d"]
        if date_range == "last_month":
            return ["newer_than:30d"]
        if date_range == "this_week":
            return ["newer_than:7d"]
        return [date_range]

    def _normalize_query_value(self, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        if is_natural_language_latest_reference(value):
            return ""
        if "@" in value or ":" in value:
            return value
        if " " in value:
            return f'"{value}"'
        return value

    def _first_match(self, patterns: list[str], text: str) -> str | None:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    def _clean_entity(self, value: str | None) -> str | None:
        if not value:
            return None
        cleaned = value.strip().strip(".,;: ")
        if is_natural_language_latest_reference(cleaned) or self._looks_like_generic_mail_reference(cleaned):
            return None
        cleaned = re.split(r"\b(?:and|then|please|summarize|analyse|analyze|report|draft|reply|write|send|sends)\b", cleaned, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if is_natural_language_latest_reference(cleaned) or self._looks_like_generic_mail_reference(cleaned):
            return None
        return cleaned or None

    def _looks_like_generic_mail_reference(self, value: str) -> bool:
        normalized = re.sub(r"\s+", " ", (value or "").strip().lower())
        if not normalized or "@" in normalized:
            return False
        if any(term in normalized for term in ("mail", "email", "message", "inbox", "thread")):
            return True
        return False

    def _resolve_contact_alias(self, value: str) -> str | None:
        lower_value = value.lower()
        for alias, email in CONTACT_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", lower_value):
                return email
        return None

    def _extract_tone(self, lower_prompt: str) -> str:
        for pattern, tone in TONE_PATTERNS:
            if re.search(pattern, lower_prompt, flags=re.IGNORECASE):
                return tone
        return "professional"

    def _normalize_prompt(self, prompt: str | None) -> str:
        normalized = (prompt or "").strip()
        if not normalized:
            return ""

        normalized = normalized.lower()
        for pattern, replacement in NORMALIZATION_RULES:
            normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    def _looks_gmail_related(self, prompt: str) -> bool:
        lower = prompt.lower()
        return any(word in lower for word in ["mail", "email", "gmail", "inbox", "message"])

    def _build_llm(self):
        if settings.OPENAI_API_KEY:
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model="gpt-4o-mini",
                temperature=0.2,
                api_key=settings.OPENAI_API_KEY,
            )

        if settings.GEMINI_API_KEY:
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(
                model="gemini-2.5-flash",
                temperature=0.2,
                google_api_key=settings.GEMINI_API_KEY,
                convert_system_message_to_human=True,
            )

        return None

    def _build_llm_prompt(self, system: str, human: str) -> ChatPromptTemplate:
        return ChatPromptTemplate.from_messages([("system", system), ("human", human)])

    def _invoke_text_model(self, prompt: ChatPromptTemplate, fallback: str) -> str:
        if self._llm is None:
            return fallback

        try:
            response = prompt | self._llm
            result = response.invoke({})
            content = getattr(result, "content", None)
            if isinstance(content, str) and content.strip():
                return content.strip()
            if result:
                return str(result).strip()
        except Exception:
            return fallback

        return fallback

    def _fallback_summary(self, text: str) -> str:
        cleaned = self._shorten(text, 600)
        return f"Summary of Gmail content:\n{cleaned}"

    def _fallback_analysis(self, text: str) -> str:
        cleaned = self._shorten(text, 800)
        return f"Analysis of Gmail content:\n{cleaned}"

    def _fallback_report(self, text: str) -> str:
        cleaned = self._shorten(text, 1000)
        return f"Analytical report from Gmail content:\n{cleaned}"

    def _fallback_reply(self, instruction: str, tone: str) -> str:
        base = instruction.strip() or "Thank you. I will check and update you."
        if tone.lower() == "professional":
            return base
        return base

    def _stringify(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return str(value)
        except Exception:
            return json.dumps(value, default=str)

    def _shorten(self, text: str, limit: int) -> str:
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3].rstrip() + "..."
