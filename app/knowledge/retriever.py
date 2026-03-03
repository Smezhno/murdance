"""KB Retriever — phase/slot-based context injection for LLM prompt.

Replaces full KB dump with relevant excerpts.
Target: < 800 tokens per retrieval. Hard cap: 3200 chars.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pymorphy3

if TYPE_CHECKING:
    from app.knowledge.base import FAQ, KnowledgeBase
    from app.core.slot_tracker import ConversationPhase
    from app.models import SlotValues

_MINIMAL_FALLBACK = "Студия She Dance, Владивосток. Уточните у администратора."
MAX_CONTEXT_CHARS = 3200

_STOP_WORDS = frozenset({
    "я", "ты", "мы", "вы", "он", "она", "оно", "они",
    "и", "в", "на", "с", "к", "по", "из", "за", "о", "об",
    "не", "ни", "но", "а", "да", "нет", "ли", "бы", "же",
    "что", "как", "где", "когда", "кто", "чем", "это",
    "мне", "мой", "свой", "тебе", "его", "её",
    "хочу", "можно", "нужно", "есть", "будет",
    "очень", "ещё", "еще", "уже", "тоже", "только",
})


@dataclass
class KBContext:
    text: str
    token_estimate: int
    sources: list[str] = field(default_factory=list)


class KBRetriever:
    def __init__(self, kb: "KnowledgeBase"):
        self._kb = kb
        self._morph = pymorphy3.MorphAnalyzer()
        self._faq_lemmas: list[tuple[set[str], "FAQ"]] = [
            (self._lemmatize(faq.q), faq) for faq in self._kb.faq
        ]
        # Pre-lemmatize trigger keywords
        self._sub_triggers = {
            self._lemma(w)
            for w in ["абонемент", "подписка", "безлимит", "пакет", "занятие"]
        }
        self._policy_triggers = {
            self._lemma(w)
            for w in ["отмена", "перенос", "опоздание", "возврат", "надеть", "взять", "принести"]
        }

    def _lemma(self, word: str) -> str:
        parsed = self._morph.parse(word.lower().strip())
        return parsed[0].normal_form if parsed else word.lower()

    def _lemmatize(self, text: str) -> set[str]:
        words = text.lower().split()
        lemmas = set()
        for w in words:
            clean = w.strip(".,!?;:()\"'-")
            if not clean or clean in _STOP_WORDS:
                continue
            parsed = self._morph.parse(clean)
            if parsed:
                lemmas.add(parsed[0].normal_form)
        return lemmas

    def retrieve(
        self,
        user_text: str,
        phase: "ConversationPhase",
        slots: "SlotValues",
    ) -> KBContext:
        sections: list[tuple[int, str, str]] = []  # (priority, text, source)

        # P0: Studio name
        sections.append((0, f"Студия: {self._kb.studio.name}", "studio"))

        # P1: Slot context (filled slots = highest priority)
        self._add_slot_context(sections, slots)

        # P2: Phase context (styles/branches if not selected)
        self._add_phase_context(sections, phase, slots)

        # P3: FAQ (lemmatized search)
        self._add_faq_context(sections, user_text, phase)

        # P4: Intent triggers (subscriptions, policies)
        user_lemmas = self._lemmatize(user_text)
        self._add_intent_context(sections, user_lemmas)

        # P5: Holiday (GREETING only)
        self._add_holiday_context(sections, phase)

        return self._assemble(sections)

    def _add_slot_context(self, sections: list, slots: "SlotValues") -> None:
        # Branch address
        if slots.branch:
            addr = None
            if hasattr(self._kb, "get_branch_address"):
                addr = self._kb.get_branch_address(slots.branch)
            if addr:
                sections.append(
                    (1, f"Филиал {slots.branch}: {addr}", f"branch.{slots.branch}")
                )

        # Style price + dress code
        if slots.group:
            svc = self._find_service(
                getattr(slots, "style_id", None), slots.group
            )
            if svc:
                price = f"{svc.price_single}₽" if svc.price_single else "уточняется"
                sections.append(
                    (1, f"{svc.name}: разовое {price}", f"service.{svc.id}")
                )
            if hasattr(self._kb, "get_dress_code"):
                dress = self._kb.get_dress_code(slots.group)
                if dress:
                    sections.append(
                        (2, f"Что надеть: {dress}", f"dress.{slots.group}")
                    )

        # Teacher info
        if slots.teacher:
            teacher = self._find_teacher(
                getattr(slots, "teacher_id", None), slots.teacher
            )
            if teacher:
                sections.append(
                    (
                        2,
                        f"{teacher.name}: {', '.join(teacher.styles)}",
                        f"teacher.{teacher.id}",
                    )
                )
        # When style is set but teacher is not (e.g. "подскажи кто ведёт") — list teachers for this style
        elif slots.group:
            teachers_for_style = self._teachers_for_style(slots.group, slots)
            if teachers_for_style:
                names = ", ".join(teachers_for_style)
                sections.append(
                    (
                        2,
                        f"Преподаватели по направлению {slots.group}: {names}",
                        "teachers_for_style",
                    )
                )

    def _teachers_for_style(self, group_name: str, slots: "SlotValues") -> list[str]:
        """Return teacher names who teach this style (KB match by group/service name)."""
        svc = self._find_service(getattr(slots, "style_id", None), group_name)
        name_or_id = svc.name if svc else group_name
        id_for_match = svc.id if svc else None
        names: list[str] = []
        for t in self._kb.teachers:
            if name_or_id in t.styles or (id_for_match and id_for_match in t.styles):
                names.append(t.name)
        return names[:10]  # cap for prompt size

    def _add_phase_context(
        self, sections: list, phase: "ConversationPhase", slots: "SlotValues"
    ) -> None:
        from app.core.slot_tracker import ConversationPhase

        if phase in (ConversationPhase.GREETING, ConversationPhase.DISCOVERY):
            if not slots.group:
                styles = [
                    f"{s.name}: {s.description[:50]}" for s in self._kb.services
                ]
                sections.append(
                    (
                        2,
                        "Направления:\n" + "\n".join(f"- {s}" for s in styles),
                        "services",
                    )
                )
            if (
                not slots.branch
                and hasattr(self._kb, "branches")
                and self._kb.branches
            ):
                names = [b.name for b in self._kb.branches[:6]]
                sections.append(
                    (2, f"Филиалы: {', '.join(names)}", "branches")
                )

    def _add_faq_context(
        self,
        sections: list,
        user_text: str,
        phase: "ConversationPhase",
    ) -> None:
        from app.core.slot_tracker import ConversationPhase

        # No FAQ in late booking phases
        if phase in (
            ConversationPhase.CONFIRMATION,
            ConversationPhase.BOOKING,
            ConversationPhase.POST_BOOKING,
        ):
            return

        query_lemmas = self._lemmatize(user_text)
        if not query_lemmas:
            return

        scored = []
        for faq_lemmas, faq in self._faq_lemmas:
            if not faq_lemmas:
                continue
            overlap = len(query_lemmas & faq_lemmas)
            if overlap > 0:
                score = overlap / min(len(query_lemmas), len(faq_lemmas))
                scored.append((score, faq))

        scored.sort(key=lambda x: -x[0])
        for score, faq in scored[:2]:
            if score >= 0.3:
                sections.append((3, f"FAQ: {faq.q}\n→ {faq.a}", "faq"))

    def _add_intent_context(
        self, sections: list, user_lemmas: set[str]
    ) -> None:
        if user_lemmas & self._sub_triggers and self._kb.subscriptions:
            lines = []
            for s in self._kb.subscriptions[:5]:
                classes_str = (
                    "безлимит" if s.classes == -1 else f"{s.classes} зан."
                )
                lines.append(f"- {s.name}: {classes_str}, {s.price}₽")
            sections.append(
                (4, "Абонементы:\n" + "\n".join(lines), "subscriptions")
            )

        if user_lemmas & self._policy_triggers and self._kb.policies:
            p = self._kb.policies
            parts = []
            if p.cancellation:
                parts.append(f"Отмена: {p.cancellation[:100]}")
            if p.trial_class:
                parts.append(f"Пробное: {p.trial_class[:100]}")
            if p.what_to_bring:
                parts.append(f"С собой: {p.what_to_bring[:100]}")
            if parts:
                sections.append((4, "\n".join(parts), "policies"))

    def _add_holiday_context(
        self, sections: list, phase: "ConversationPhase"
    ) -> None:
        from app.core.slot_tracker import ConversationPhase
        from datetime import date

        if phase != ConversationPhase.GREETING:
            return
        today = date.today().isoformat()
        for h in self._kb.holidays:
            if h.from_date <= today <= h.to_date:
                sections.append((1, f"⚠️ {h.name}: {h.message}", "holiday"))
                break

    def _assemble(self, sections: list) -> KBContext:
        sections.sort(key=lambda x: x[0])
        parts, sources = [], []
        total = 0
        for priority, text, source in sections:
            text_len = len(text)
            if total + text_len > MAX_CONTEXT_CHARS:
                if priority <= 1:
                    remaining = MAX_CONTEXT_CHARS - total
                    if remaining > 100:
                        parts.append(text[:remaining])
                        sources.append(source + ".truncated")
                        total += remaining
                continue
            parts.append(text)
            sources.append(source)
            total += text_len

        text = "\n\n".join(parts) if parts else _MINIMAL_FALLBACK
        if not parts:
            sources = ["fallback"]
        return KBContext(text=text, token_estimate=len(text) // 4, sources=sources)

    def _find_service(self, style_id: object, group_name: str):
        if style_id is not None:
            for s in self._kb.services:
                if str(s.id) == str(style_id):
                    return s
        for s in self._kb.services:
            if s.name.lower() == group_name.lower():
                return s
        return None

    def _find_teacher(self, teacher_id: object, teacher_name: str):
        if teacher_id is not None:
            for t in self._kb.teachers:
                if str(t.id) == str(teacher_id):
                    return t
        for t in self._kb.teachers:
            if t.name.lower() == teacher_name.lower():
                return t
        return None
