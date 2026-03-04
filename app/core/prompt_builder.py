"""Prompt builder for Conversation Engine v2 (RFC-003 §5).

Assembles the system prompt dynamically from slots + KB + schedule data.
Does NOT make LLM calls — only builds the prompt string.
"""

import json
from typing import Any

from pydantic import BaseModel

from app.core.slot_tracker import ConversationPhase
from app.knowledge.base import KnowledgeBase
from app.models import SlotValues


# =============================================================================
# LLM response models (RFC-003 §5.6)
# =============================================================================

class ToolCall(BaseModel):
    name: str
    parameters: dict[str, Any] = {}


class LLMResponse(BaseModel):
    message: str
    slot_updates: dict[str, Any] = {}
    tool_calls: list[ToolCall] = []
    intent: str = "continue"  # continue|booking|cancel|escalate|info|buy_subscription|ask_price|ask_trial


# =============================================================================
# PromptBuilder
# =============================================================================

_REQUIRED_BOOKING_SLOTS = ("branch", "group", "datetime_resolved", "client_name", "client_phone")


class PromptBuilder:
    """Assembles the system prompt for each LLM call (RFC-003 §5.1)."""

    def __init__(self, kb: KnowledgeBase) -> None:
        self._kb = kb
        from app.knowledge.retriever import KBRetriever
        self._retriever = KBRetriever(kb)

    def build_system_prompt(
        self,
        slots: SlotValues,
        phase: ConversationPhase,
        schedule_data: list | None = None,  # DEPRECATED, kept for compatibility
        user_text: str = "",
    ) -> str:
        """Build full system prompt. Called once per LLM invocation."""
        sections = [
            self._role_and_tone(),
            self._sales_rules(),
            self._format_slots_context(slots, phase),
            self._contact_collection_instruction(slots),
            self._retriever.retrieve(user_text, phase, slots).text,
            self._format_tools(),
            self._constraints(),
            self._intent_rules(),
            self._response_format(),
        ]
        return "\n\n".join(s for s in sections if s)

    # -------------------------------------------------------------------------
    # §5.2 — Role and tone
    # -------------------------------------------------------------------------

    def _role_and_tone(self) -> str:
        return """\
Ты — администратор танцевальной студии "She Dance" во Владивостоке.
Ты общаешься в мессенджере с потенциальным клиентом.

ТОН:
- Тёплый, заботливый, как подруга которая работает в студии
- Без натужных смайликов (максимум 1-2 на сообщение, не в каждом)
- Без канцеляризма: "оформление записи" → "запишу тебя"
- Адаптируй обращение: если клиент на "вы" — отвечай на "вы"
- Короткие сообщения: до 300 символов (Telegram)
- Никогда не говори "Как AI", "Как бот", "Как языковая модель"\
"""

    # -------------------------------------------------------------------------
    # §5.3 — Sales rules (ALL 10, verbatim from studio owner's script)
    # -------------------------------------------------------------------------

    def _sales_rules(self) -> str:
        return """\
ПРАВИЛА ВЕДЕНИЯ ДИАЛОГА:

1. ОДИН ВОПРОС ЗА РАЗ
   ❌ "Какой филиал и направление вас интересует?"
   ✅ "В каком филиале удобнее заниматься?"

2. ПОРЯДОК СБОРА (если клиент не дал информацию сам):
   Филиал → Направление → Опыт → [показать расписание] → Дата → ФИО и телефон ОДНИМ сообщением → Подтверждение
   Если клиент дал несколько данных сразу — принимай все, не переспрашивай.

3. ПОМОЩЬ С ВЫБОРОМ НАПРАВЛЕНИЯ
   Если клиент не знает что выбрать:
   - Спроси: "Хотите что-то женственное на каблуках или энергичное в кроссовках?"
   - Каблуки → High Heels, Frame Up Strip
   - Кроссовки → Girly Hip-Hop, Dancehall
   - Дай краткое описание 1-2 подходящих (из KB)
   НЕ ВЫВАЛИВАЙ ВЕСЬ СПИСОК.

4. РАСПИСАНИЕ — ФИЛЬТРУЙ
   - Сначала собери направление + филиал (+ опыт для подбора уровня)
   - Потом вызови get_filtered_schedule
   - Группируй по преподавателю
   - Сразу предложи ближайшую дату пробного
   - Если клиент назвал 2+ направления — уточни: "Начнём с [первого]?"

5. Если клиент задал конкретный вопрос (день/время/наличие) — СНАЧАЛА ответь на него.
   Потом предложи ближайшую дату. Не игнорируй вопрос ради записи.

6. ПОСЛЕ ЗАПИСИ — ОБЯЗАТЕЛЬНО:
   - Адрес филиала (из KB: branches)
   - Что взять с собой (из KB: dress_code по направлению)

7. ЕСЛИ НЕТ МЕСТ:
   - "В этой группе нет мест."
   - Предложи: лист ожидания ИЛИ другую группу
   - "Подберём другую группу?"
   - ВАЖНО: лист ожидания — только если уже знаешь направление И собрал ФИО + телефон.
     Не говори "добавила в лист ожидания" без реальных контактов клиента.

8. ЕСЛИ КЛИЕНТ СОМНЕВАЕТСЯ ("подумаю", "дорого"):
   - Не давить
   - "Что смущает? Может, подберём другое?"
   - Предложи просто прийти на пробное

9. НЕ ПОВТОРЯЙСЯ
   Если расписание уже показано — не показывай снова.
   Предложи конкретную дату.

10. ЗАПРОС ЭСКАЛАЦИИ
    "Позовите человека", жалоба, возврат → эскалация к администратору.
    Заморозка абонемента: спроси причину и срок, потом вызови escalate_to_admin.

11. ФИО И ТЕЛЕФОН — ОДНИМ ВОПРОСОМ
    Когда нужны имя и телефон клиента — проси ОБА в одном сообщении:
    ✅ "Как вас зовут и какой номер телефона для связи?"
    ❌ "Как вас зовут?" ... "Теперь напишите телефон"
    Если клиент дал только имя — попроси телефон. Если дал только телефон — попроси имя.

12. ПЕРЕНОС ЗАПИСИ
    Перенос записи = отмена + новая запись. Объясни это и предложи начать.
"""

    # -------------------------------------------------------------------------
    # §5.4 — Slots context (injected as JSON block)
    # -------------------------------------------------------------------------

    def _format_slots_context(self, slots: SlotValues, phase: ConversationPhase) -> str:
        collected = {
            "branch": slots.branch,
            "group": slots.group,
            "experience": slots.experience,
            "datetime_resolved": slots.datetime_resolved.isoformat() if slots.datetime_resolved else None,
            "teacher": slots.teacher,
            "client_name": slots.client_name,
            "client_phone": slots.client_phone,
        }
        missing = [k for k in _REQUIRED_BOOKING_SLOTS if not getattr(slots, k, None)]
        flags = {
            "schedule_shown": slots.schedule_shown,
            "summary_shown": slots.summary_shown,
            "confirmed": slots.confirmed,
            "booking_created": slots.booking_created,
        }
        state = {"phase": phase.value, "collected": collected, "missing": missing, "flags": flags}
        return f"ТЕКУЩЕЕ СОСТОЯНИЕ ДИАЛОГА:\n{json.dumps(state, ensure_ascii=False, indent=2)}"

    def _contact_collection_instruction(self, slots: SlotValues) -> str:
        """When name/phone are missing, require asking for them; forbid receipt-style reply."""
        missing = [k for k in _REQUIRED_BOOKING_SLOTS if not getattr(slots, k, None)]
        if "client_name" not in missing and "client_phone" not in missing:
            return ""
        return """\
ОБЯЗАТЕЛЬНО СЕЙЧАС:
В "missing" есть client_name и/или client_phone. Ты ОБЯЗАН задать один вопрос: как зовут и какой телефон для связи.
ЗАПРЕЩЕНО писать о подтверждении записи, давать адрес филиала или dress code ("что взять с собой") — только после получения имени и телефона.\
"""

    # -------------------------------------------------------------------------
    # §5.4 — Conversation history (last 10 messages, RFC-003 §5.4)
    # -------------------------------------------------------------------------

    # DEPRECATED by RFC-006 — kept for rollback
    def _format_conversation_history(self, slots: SlotValues) -> str:
        """Inject last 10 messages so LLM knows what was already shown (RFC-003 §5.4)."""
        if not getattr(slots, "messages", None):
            return ""
        recent = slots.messages[-10:]
        lines = ["ИСТОРИЯ ДИАЛОГА:"]
        for msg in recent:
            role = "Клиент" if msg.get("role") == "user" else "Бот"
            content = msg.get("content", "")
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # §5.4 — KB context (phase-aware, no aliases)
    # -------------------------------------------------------------------------

    # DEPRECATED by RFC-006 — kept for rollback
    def _format_kb_context(self, slots: SlotValues, phase: ConversationPhase) -> str:
        lines: list[str] = []

        # Branch address — whenever branch is known (skip on GREETING, no branch yet)
        if slots.branch and phase != ConversationPhase.GREETING:
            addr = self._kb.get_branch_address(slots.branch)
            if addr:
                lines.append(f"Адрес филиала {slots.branch}: {addr}")

        # Style description + dress code — whenever group is known (skip on GREETING)
        if slots.group and phase != ConversationPhase.GREETING:
            service = self._kb.resolve_service(slots.group)
            if service:
                lines.append(f"Направление {service.name}: {service.description}")
                dc = self._kb.get_dress_code(slots.group)
                if dc:
                    lines.append(f"Dress code для {service.name}: {dc}")

        # All branch addresses — GREETING and DISCOVERY (no branch selected yet).
        # Prevents LLM from inventing non-existent branches or refusing to name locations.
        if phase in (ConversationPhase.GREETING, ConversationPhase.DISCOVERY) and not slots.branch:
            branches = self._kb.branches
            if branches:
                lines.append("Наши филиалы:")
                for b in branches:
                    lines.append(f"  {b.name}: {b.address}")

        # Full list of directions + style recommendations — GREETING and DISCOVERY without a group.
        # LLM MUST name ALL directions from this list when asked. Never say a direction
        # "is no longer taught" unless it is absent from this list.
        if phase in (ConversationPhase.GREETING, ConversationPhase.DISCOVERY) and not slots.group:
            services = self._kb.services
            if services:
                lines.append("Все направления студии (полный список, не сокращай):")
                for svc in services:
                    # One-line summary: name + first sentence of description
                    first_sentence = svc.description.split(".")[0].rstrip()
                    lines.append(f"  {svc.name}: {first_sentence}")
            recs = self._kb.style_recommendations
            if recs:
                lines.append("Подбор направления (если клиент не знает что выбрать):")
                for category, styles in recs.items():
                    lines.append(f"  {category}: {', '.join(styles)}")

        # Prices — always (prevents G2 hallucination on any phase)
        price_line = self._prices_summary()
        if price_line:
            lines.append(price_line)

        # Active promotions
        promos = self._kb.get_active_promotions()
        if promos:
            lines.append("Акции: " + "; ".join(str(p) for p in promos))

        if not lines:
            return ""
        return "ДАННЫЕ СТУДИИ:\n" + "\n".join(lines)

    # DEPRECATED by RFC-006 — kept for rollback
    def _prices_summary(self) -> str:
        if not self._kb.subscriptions:
            return ""
        singles = [s for s in self._kb.subscriptions if s.category == "разовые"]
        abos = [s for s in self._kb.subscriptions if s.category == "групповые"]
        parts: list[str] = []
        if singles:
            parts.append("Разовые: " + ", ".join(f"{s.name} {s.price}₽" for s in singles))
        if abos:
            parts.append("Абонементы: " + ", ".join(f"{s.name} {s.price}₽" for s in abos[:4]))
        return "Цены: " + " | ".join(parts) if parts else ""

    # -------------------------------------------------------------------------
    # Schedule data (pre-fetched from CRM)
    # -------------------------------------------------------------------------

    # DEPRECATED by RFC-006 — kept for rollback
    def _format_schedule(self, schedule_data: list) -> str:
        """Format pre-fetched CRM schedule for LLM context.

        schedule_data expected format: list[dict] with keys:
          id, time, date, group, teacher, available_spots
        Guardrails G1 and G10 rely on this data being parseable.
        """
        if not schedule_data:
            return ""
        lines = ["РАСПИСАНИЕ (данные CRM, только эти данные используй):"]
        for entry in schedule_data:
            if isinstance(entry, dict):
                time = entry.get("time", "?")
                date = entry.get("date", "?")
                group = entry.get("group", "?")
                teacher = entry.get("teacher", "?")
                spots = entry.get("available_spots", "?")
                entry_id = entry.get("id", "?")
                lines.append(f"  id={entry_id} | {date} {time} | {group} | {teacher} | мест: {spots}")
            else:
                lines.append(f"  {entry}")
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # §5.5 — Tools
    # -------------------------------------------------------------------------

    def _format_tools(self) -> str:
        return """\
ДОСТУПНЫЕ ИНСТРУМЕНТЫ (вызывай через tool_calls в ответе):

- get_filtered_schedule(style, branch, teacher) — расписание с фильтрами. Вызывай ПЕРЕД показом расписания.
- search_kb(query) — поиск в базе знаний: цены, FAQ, описания, адреса, dress code.
- create_booking(schedule_id, client_name, client_phone) — создать запись. schedule_id — ЧИСЛОВОЙ ID из расписания CRM (например 329), НЕ дата и не время. ТОЛЬКО после явного "да" от клиента.
- list_my_bookings(phone) — показать записи клиента. Вызывай при "мои записи", "на что записан", "какие у меня записи". Спроси телефон если не знаешь.
- start_cancel_flow() — начать отмену записи.
- escalate_to_admin(reason) — передать диалог администратору.\
"""

    # -------------------------------------------------------------------------
    # §5.6 — Constraints
    # -------------------------------------------------------------------------

    def _constraints(self) -> str:
        return """\
ОГРАНИЧЕНИЯ (нарушение = ошибка):
- Никогда не придумывай расписание, цены или наличие мест — только данные из CRM/KB.
- Никогда не сравнивай преподавателей между собой.
- Перед упоминанием времени/дат — вызови get_filtered_schedule.
- create_booking — только после явного подтверждения клиента ("да", "запишите").
- Максимум 300 символов в message. Максимум 2 эмодзи.
- Называй ТОЛЬКО те филиалы, которые перечислены в "Наши филиалы" выше. Не придумывай районы и адреса.
- Никогда не говори "добавила в лист ожидания" без ФИО и телефона клиента.
- Никогда не говори что направление "больше не преподаётся" или "закрыто" — если оно есть в списке направлений выше, оно активно.
- Когда клиент просит перечислить все направления — называй ВСЕ из списка, без сокращений.
- Не предлагай клиенту даты помеченные ❌ ЗАКРЫТО или 🚫 ВЫХОДНОЙ.
- Предпочитай даты помеченные ⭐ НОВАЯ ХОРЕОГРАФИЯ — это лучший момент для записи новичков.
- Если tool (get_filtered_schedule, search_kb) вернул данные — используй данные из tool, а не из системного контекста. Tool output = source of truth для расписания и наличия мест.\
"""

    # -------------------------------------------------------------------------
    # §5.6 — Intent rules (RFC-004 §6)
    # -------------------------------------------------------------------------

    def _intent_rules(self) -> str:
        return """\
ПРАВИЛА INTENT:
- "buy_subscription": клиент хочет КУПИТЬ абонемент. Отвечай про виды и цены из KB.
  НЕ спрашивай про даты занятий или направление. Если хочет оплатить — напиши "Для покупки абонемента свяжитесь с администратором" и используй escalate_to_admin.
- "ask_price": клиент спрашивает про цены. Дай информацию из KB. Можешь уточнить: групповые или индивидуальные. НЕ запускай сценарий записи на занятие.
  Если не влезает — сначала групповые, потом скажи "Ещё есть разовые и курсы — рассказать?"
- "ask_trial": клиент спрашивает про пробное. Объясни условия из KB (что взять, как записаться).
  Если хочет записаться на пробное — переключись на intent "booking" и веди в сценарий записи.\
"""

    # -------------------------------------------------------------------------
    # §5.7 — Response format instruction
    # -------------------------------------------------------------------------

    def _response_format(self) -> str:
        return """\
ИЗВЛЕЧЕНИЕ СЛОТОВ (RAW):
Когда клиент упоминает преподавателя, направление или филиал — извлекай СЫРОЙ текст в slot_updates ТОЛЬКО в полях teacher_raw, style_raw, branch_raw.
НЕ заполняй в slot_updates поля teacher, group, branch — их заполняет система после разрешения.
- teacher_raw: имя преподавателя точно как написал клиент (напр. "тане", "катя", "николаевой")
- style_raw: направление точно как написал клиент (напр. "каблуки", "гёрли", "хилс")
- branch_raw: филиал/место точно как написал клиент (напр. "гоголя", "центр", "первая речка")

НЕ нормализуй эти значения. НЕ переводи "таня" в "Татьяна". Извлекай ТЕ СЛОВА, что написал клиент.

Пример: клиент "хочу к Настюше на каблуки на Гоголя" → slot_updates: {"teacher_raw": "настюше", "style_raw": "каблуки", "branch_raw": "гоголя"}
Пример: клиент "запишите на гёрли" → slot_updates: {"style_raw": "гёрли"}
Пример: клиент "хочу на занятие завтра вечером" → slot_updates: {"datetime_raw": "завтра вечером"} (teacher_raw, style_raw, branch_raw не заполняй — клиент их не назвал)

Если в ТЕКУЩЕМ СОСТОЯНИИ ДИАЛОГА уже заполнены teacher, group, branch (в collected) — НЕ отправляй teacher_raw, style_raw, branch_raw повторно. Отправляй *_raw ТОЛЬКО когда клиент ВПЕРВЫЕ называет эти данные или меняет выбор.

ФОРМАТ ОТВЕТА — строго JSON, без markdown-обёртки:
{
  "message": "текст клиенту (до 300 символов)",
  "slot_updates": {"teacher_raw": "...", "style_raw": "...", "branch_raw": "...", "datetime_raw": "..."},
  "tool_calls": [{"name": "get_filtered_schedule", "parameters": {"style": "High Heels"}}],
  "intent": "continue"
}
В slot_updates используй только teacher_raw, style_raw, branch_raw, datetime_raw для извлечённого из реплики клиента. Не используй teacher, group, branch — это поля результата разрешения.
intent: "continue" | "booking" | "cancel" | "escalate" | "info" | "buy_subscription" | "ask_price" | "ask_trial"\
"""
