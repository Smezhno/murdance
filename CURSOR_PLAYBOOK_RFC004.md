# Cursor Playbook — RFC-004: Entity Resolver

> **Этот playbook — серия промптов для реализации RFC-004.**
> Зависит от RFC-003 (Conversation Engine v2) для Phase 4.3.
> Phases 4.1–4.2 можно выполнять параллельно с RFC-003.

## Золотые правила

1. **Один модуль за раз.** Cursor галлюцинирует на третьем файле.
2. **Сначала план, потом код.** "Propose structure, don't write code yet."
3. **Явные ссылки на RFC-004.** Не "сделай resolver", а "implement per RFC-004 §4.1".
4. **Проверяй каждый модуль** — unit-тесты до следующего шага.
5. **Не ломай то что работает.** cancel_flow.py, schedule_flow.py, idempotency.py, temporal.py — не трогай.
6. **EntityResolver — детерминированный код.** Никакого LLM внутри resolver'ов.
7. **Exact match first, pymorphy3 second.** Не вызывать pymorphy3 если exact lookup нашёл результат.

---

## Pre-flight: обновление cursorrules

### Промпт 0.1 — Обновить cursorrules
```
Read RFC_004_ENTITY_RESOLVER.md sections 3 and 8.

Update the `cursorrules` file:

1. In Project Structure, add to core/:
   ```
   core/
   ├── entity_resolver/
   │   ├── __init__.py           # Exports: EntityResolver protocol, ResolvedEntity, models
   │   ├── protocol.py           # EntityResolver Protocol (interface)
   │   ├── models.py             # ResolvedEntity, ResolvedEntities dataclasses
   │   ├── teacher_resolver.py   # TeacherResolver: names_dict + CRM sync + pymorphy3
   │   ├── branch_resolver.py    # BranchResolver: aliases from studio.yaml
   │   ├── style_resolver.py     # StyleResolver: aliases from studio.yaml
   │   ├── alias_resolver.py     # AliasEntityResolver: implements Protocol
   │   └── names_dict.json       # Static dictionary of Russian name diminutives (~300 names)
   ```

2. Add to Architecture Rules:
   "Entity Resolver: deterministic normalization of user input to CRM IDs. 
    No LLM inside resolvers. Exact match first, pymorphy3 fallback for case forms only. 
    See RFC-004."

3. Add to Core Principles:
   "LLM extracts raw slot values (teacher_raw, style_raw, branch_raw). 
    EntityResolver normalizes raw values to CRM IDs. 
    Resolver does NOT parse user text — only normalizes pre-extracted values."

Do not write any other code. Only update cursorrules.
```

---

## Phase 4.1: Фундамент — модели и словарь

### Промпт 4.1.1 — Модели данных + Protocol
```
Read RFC_004_ENTITY_RESOLVER.md sections 4.6 ("Единая точка входа") and 5.3 ("Новые поля в SlotValues").

Task: Create data models and protocol for the Entity Resolver system.

Step 1 — PLAN ONLY (no code yet):
- Show the ResolvedEntity and ResolvedEntities dataclasses
- Show the EntityResolver Protocol with method signatures
- Show which fields you will add to SlotValues in app/models.py
- Confirm all new SlotValues fields have defaults (backward compat with existing PG sessions)

Step 2 — After plan approval, implement:

File 1: app/core/entity_resolver/__init__.py — NEW file:
  - Export: EntityResolver, ResolvedEntity, ResolvedEntities

File 2: app/core/entity_resolver/models.py — NEW file, under 40 lines:
  @dataclass ResolvedEntity:
    - name: str              (canonical name)
    - crm_id: int | str      (CRM ID)
    - entity_type: str       ("teacher" | "branch" | "style")
    - confidence: float      (1.0 for exact match — fuzzy is OFF in MVP)
    - source: str            ("alias" | "names_dict" | "direct")

  @dataclass ResolvedEntities:
    - teachers: list[ResolvedEntity]
    - branches: list[ResolvedEntity]
    - styles: list[ResolvedEntity]
    - unknown_area: str | None = None

File 3: app/core/entity_resolver/protocol.py — NEW file, under 30 lines:
  class EntityResolver(Protocol):
    async def resolve_teacher(self, raw: str, tenant_id: str) -> list[ResolvedEntity]
    async def resolve_branch(self, raw: str, tenant_id: str) -> list[ResolvedEntity]
    async def resolve_style(self, raw: str, tenant_id: str) -> list[ResolvedEntity]
    async def check_unknown_area(self, raw: str, tenant_id: str) -> str | None

  IMPORTANT: There is NO resolve_all method. Engine calls each resolver separately.
  Each method receives a PRE-EXTRACTED value from LLM, not raw user text.

File 4: app/models.py — Add to SlotValues (keep ALL existing fields):
  # Raw values from LLM extraction (before normalization)
  teacher_raw: str | None = None
  style_raw: str | None = None
  branch_raw: str | None = None

  # CRM IDs after EntityResolver normalization  
  teacher_id: int | str | None = None
  branch_id: str | None = None
  style_id: int | str | None = None

  ALL fields MUST have default = None.

File 5: tests/unit/test_entity_models.py — Tests:
  - ResolvedEntity creation and fields
  - ResolvedEntities with empty lists
  - SlotValues backward compat: SlotValues() with no new fields works
  - SlotValues with new fields serializes/deserializes from JSONB

Do NOT touch any other files.
```

### Промпт 4.1.2 — names_dict.json (словарь русских имён)
```
Task: Create a static dictionary of Russian given names with their diminutive forms.

Create File: app/core/entity_resolver/names_dict.json

Requirements:
- Format: {"canonical_name": ["diminutive1", "diminutive2", ...]}
- All names and diminutives in lowercase
- Cover ~300 most common Russian names (both male and female)
- Each canonical name should have 2-8 diminutive forms

MUST include at minimum these names with their diminutives:
  "александр": ["саша", "саня", "шура", "шурик", "алекс"]
  "александра": ["саша", "сашенька", "шура", "алекса"]
  "анастасия": ["настя", "настена", "настенька", "настюша", "ася", "стася"]
  "анна": ["аня", "анечка", "аннушка", "нюра", "нюша"]
  "валерия": ["лера", "лерочка", "валера"]
  "виктория": ["вика", "вики"]
  "дарья": ["даша", "дашенька", "дашуля"]
  "евгения": ["женя", "женечка"]
  "екатерина": ["катя", "катюша", "катерина", "катенька"]
  "елена": ["лена", "леночка"]
  "ирина": ["ира", "ирочка", "иришка"]
  "кристина": ["кристя", "кристюша", "крис"]
  "ксения": ["ксюша", "ксюшенька"]
  "мария": ["маша", "машенька", "маруся", "маня"]
  "наталья": ["наташа", "ната", "наталия"]
  "ольга": ["оля", "оленька", "олюшка"]
  "полина": ["поля", "полюшка", "полинка"]
  "светлана": ["света", "светочка"]
  "татьяна": ["таня", "танюша", "танечка"]
  "юлия": ["юля", "юлечка"]
  
  Also include male names:
  "алексей": ["лёша", "алёша", "лёха"]
  "андрей": ["андрюша", "андрюха"]
  "дмитрий": ["дима", "димочка", "митя"]
  "евгений": ["женя", "женечка"]
  "иван": ["ваня", "ванечка", "ванюша"]
  "максим": ["макс", "максик"]
  "михаил": ["миша", "мишенька"]
  "николай": ["коля", "колюня"]
  "сергей": ["серёжа", "серёга"]

IMPORTANT:
- "женя" is a diminutive for BOTH "евгений" and "евгения" — this is correct
- "саша" is a diminutive for BOTH "александр" and "александра" — this is correct
- These conflicts are EXPECTED. The inverted index will map them to multiple canonicals.

Add at least 280 names total. Include less common names too:
  "алина", "арина", "вероника", "диана", "злата", "карина", "лилия", 
  "марина", "надежда", "оксана", "регина", "снежана", "тамара", "элина",
  "артём", "борис", "вадим", "георгий", "денис", "кирилл", "олег", "роман", 
  "тимур", "фёдор"

Do NOT create any Python code in this prompt. Only the JSON file.
Validate: no duplicate keys, all lowercase, no empty arrays.
```

### Промпт 4.1.3 — TeacherResolver
```
Read RFC_004_ENTITY_RESOLVER.md section 4.1 ("TeacherResolver") and 4.2 ("names_dict.json") and 4.3 ("CRM Teacher Sync").

Task: Implement TeacherResolver — resolves diminutive/colloquial teacher names to CRM IDs.

Step 1 — PLAN ONLY:
- Show the class structure and method signatures
- Show the resolve algorithm (exact → pymorphy3 → surname → not found)
- Show the sync_teachers flow
- Confirm: NO fuzzy matching in MVP
- Confirm: exact match checked BEFORE pymorphy3

Step 2 — After approval:

File 1: app/core/entity_resolver/teacher_resolver.py — NEW file, under 150 lines:

  class TeacherResolver:
      def __init__(self, names_dict_path: Path)
      
      async def sync(self, crm_adapter) -> None:
          """
          1. Call CRM: teacher/list → list of {id, name}
          2. For each teacher:
             a. Split name: "Анастасия Николаева" → first="анастасия", last="николаева"
             b. Look up first name in names_dict → get diminutives
             c. Build lookup: each diminutive → [{full_name, crm_id}]
             d. Add first name as alias
             e. Add last name as alias (both genders — see _build_surname_aliases)
             f. Add full name as alias
          3. Store lookup as self._lookup: dict[str, list[ResolvedEntity]]
          
          IMPORTANT on surname gender (RFC-004 P0-4 fix):
          For "николаева": also add "николаев" to lookup
          For "козлов": also add "козлова" to lookup
          Use pymorphy3 inflect({masc})/inflect({femn}) to generate opposite gender form.
          """
      
      def resolve(self, raw: str, tenant_id: str) -> list[ResolvedEntity]:
          """
          Algorithm (EXACT FIRST, pymorphy3 FALLBACK):
          
          1. normalized = raw.strip().lower()
          2. if normalized in self._lookup: return self._lookup[normalized]  # O(1)
          3. lemma = pymorphy3.parse(normalized)[0].normal_form
          4. if lemma in self._lookup: return self._lookup[lemma]
          5. return []  # Not found
          
          NO fuzzy matching. NO n-gram tokenization.
          """
      
      @property
      def is_synced(self) -> bool:
          """True if sync completed successfully at least once."""

  Internal methods:
      _load_names_dict(path) → dict[str, list[str]]    # canonical → diminutives
      _build_inverted_index() → dict[str, list[str]]    # diminutive → [canonical]
      _build_surname_aliases(surname: str, crm_id, full_name) → dict  # both gender forms

  IMPORTANT constraints:
  - pymorphy3 is imported and used ONLY inside resolve(), not during sync
  - sync() builds lookup from names_dict + CRM data, purely string operations
  - resolve() checks exact match FIRST, pymorphy3 only if exact fails
  - The inverted names_dict index maps alias → list[canonical] (NOT alias → single canonical)
    because "саша" → ["александр", "александра"] is valid

File 2: tests/unit/test_teacher_resolver.py — Tests:

  Setup: MockCRM with teachers:
    [{"id": 1, "name": "Анастасия Николаева"},
     {"id": 2, "name": "Екатерина Петрова"},
     {"id": 3, "name": "Ольга Сидорова"}]

  test_diminutive():
    resolve("настя") → [ResolvedEntity(name="Анастасия Николаева", crm_id=1)]
  
  test_case_forms():
    for form in ["насте", "настю", "настей", "настюше"]:
      resolve(form) → len >= 1, all have crm_id=1
  
  test_full_name():
    resolve("анастасия") → crm_id=1
  
  test_surname():
    resolve("николаева") → crm_id=1
  
  test_surname_gender():
    # CRM has "Анастасия Николаева" (female form)
    resolve("николаев") → crm_id=1  # male form also works
  
  test_unknown():
    resolve("вася") → []
  
  test_ambiguous():
    # Add second Анастасия to mock CRM
    resolve("настя") → len == 2
  
  test_not_synced():
    # Before sync: resolve returns []
    resolver = TeacherResolver(names_dict_path)
    assert resolver.resolve("настя") == []
    assert resolver.is_synced == False
  
  test_case_insensitive():
    resolve("НАСТЯ") → same as resolve("настя")

pip install pymorphy3 --break-system-packages if needed.

Do NOT modify any existing files. Do NOT touch cancel_flow.py, schedule_flow.py.
```

---

## Phase 4.2: Branch/Style Resolvers + KB

### Промпт 4.2.1 — BranchResolver
```
Read RFC_004_ENTITY_RESOLVER.md section 4.4 ("BranchResolver").

Task: Implement BranchResolver — resolves colloquial area/building names to branch CRM IDs.

Step 1 — PLAN ONLY:
- Show the class structure
- Show how aliases are loaded from studio.yaml
- Show how "центр" → [Алеутская, Семёновская] (multiple results)
- Show the unknown_area check
- Confirm: branch aliases checked BEFORE unknown_areas (priority rule)

Step 2 — After approval:

File 1: app/core/entity_resolver/branch_resolver.py — NEW file, under 100 lines:

  class BranchResolver:
      def __init__(self, kb: KnowledgeBase)
      
      def resolve(self, raw: str, tenant_id: str) -> list[ResolvedEntity]:
          """
          1. normalized = raw.strip().lower()
          2. Exact match in alias lookup → return results
          3. pymorphy3 normal_form → try again (for "первой речке" → "первая речка")
          4. return []
          
          Exact match FIRST. pymorphy3 FALLBACK.
          """
      
      def check_unknown_area(self, raw: str, tenant_id: str) -> str | None:
          """
          Called ONLY if resolve() returned empty list.
          Check if raw matches an unknown_area alias.
          Returns the area name if found, None otherwise.
          """

  The alias lookup is built from studio.yaml branches[].aliases:
    "гоголя" → [ResolvedEntity(name="Гоголя", crm_id="XX")]
    "центр" → [ResolvedEntity(name="Алеутская", crm_id="YY"), 
                ResolvedEntity(name="Семёновская", crm_id="ZZ")]

  IMPORTANT: "центр" deliberately maps to TWO branches. This is correct.
  Engine will ask user to choose.

  Priority rule: branch aliases → unknown_areas (NEVER the reverse).

File 2: tests/unit/test_branch_resolver.py — Tests:

  Setup: Mock KB with branches from RFC-004 §4.4:
    Гоголя (aliases: гоголя, красного знамени, первая речка, ...)
    Алеутская (aliases: алеутская, центр, родина, клевер, ...)
    Семёновская (aliases: семёновская, центр, изумруд, лотте, ...)
    Черемуховая (aliases: черемуховая, чуркин, чайка, ...)

  Unknown areas: вторая речка, баляева, заря, седанка, бам, патрокл

  test_exact_name():       resolve("гоголя") → [Гоголя]
  test_alias():            resolve("первая речка") → [Гоголя]
  test_ambiguous_center(): resolve("центр") → [Алеутская, Семёновская] (len == 2)
  test_case_insensitive(): resolve("ГОГОЛЯ") → [Гоголя]
  test_case_form():        resolve("первой речке") → [Гоголя] (via pymorphy3)
  test_unknown():          resolve("что-то") → []
  test_unknown_area():     check_unknown_area("седанка") → "седанка"
  test_not_unknown_area(): check_unknown_area("гоголя") → None (it's a branch, not unknown)
  test_branch_before_unknown(): 
    # If somehow an alias is in both (shouldn't happen with validation) 
    # → branch wins

Do NOT modify any existing files.
```

### Промпт 4.2.2 — StyleResolver
```
Read RFC_004_ENTITY_RESOLVER.md section 4.5 ("StyleResolver").

Task: Implement StyleResolver — resolves colloquial style names to CRM style IDs.

File 1: app/core/entity_resolver/style_resolver.py — NEW file, under 80 lines:

  class StyleResolver:
      def __init__(self, kb: KnowledgeBase)
      
      def resolve(self, raw: str, tenant_id: str) -> list[ResolvedEntity]:
          """
          1. normalized = raw.strip().lower()
          2. Exact match in alias lookup → return
          3. pymorphy3 normal_form → try again ("каблуках" → "каблуки")
          4. return []
          """

  Alias lookup built from studio.yaml style_aliases:
    "хилс" → [ResolvedEntity(name="High Heels", crm_id=5)]
    "каблуки" → [ResolvedEntity(name="High Heels", crm_id=5)]
    "гёрли" → [ResolvedEntity(name="Girly Hip-Hop", crm_id=8)]

File 2: tests/unit/test_style_resolver.py — Tests:

  test_alias():      resolve("каблуки") → [High Heels]
  test_slang():      resolve("гёрли") → [Girly Hip-Hop]
  test_case_form():  resolve("каблуках") → [High Heels] (via pymorphy3)
  test_english():    resolve("heels") → [High Heels]
  test_unknown():    resolve("балет") → []
  test_case():       resolve("ХИЛС") → [High Heels]

Do NOT modify any existing files.
```

### Промпт 4.2.3 — Обновление studio.yaml + KB валидация
```
Read RFC_004_ENTITY_RESOLVER.md sections 4.4, 4.5, and 7 ("Единый источник филиалов").

Task: Update studio.yaml with branch aliases, style aliases, unknown areas. 
Update KB validator. Fix the single-source-of-truth for branches.

Step 1 — PLAN ONLY:
- Show the new YAML sections you will add
- Show which validation checks you will add
- Confirm: you will MERGE studio.branches into the top-level branches section (not duplicate)
- Confirm: all branch aliases are lowercase

Step 2 — After approval:

File 1: knowledge/studio.yaml — Add/update these sections:

  branches: (replace existing, merge with studio.branches if they differ)
    Each branch MUST have:
      - id: string
      - name: string
      - crm_branch_id: string
      - address: string
      - aliases: list[string] (all lowercase)
    
    Use the exact branch data from RFC-004 §4.4:
      Гоголя, Алеутская, Семёновская, Черемуховая, Тест
    
    IMPORTANT: Include "Тест" branch — this fixes the bug where bot said "нет филиала Тест".

  style_aliases:
    Use data from RFC-004 §4.5:
      High Heels, Girly Hip-Hop, Contemporary, Frame Up Strip, Dancehall
    Each has: crm_style_id, aliases[]

  unknown_areas:
    aliases: ["вторая речка", "баляева", "заря", "седанка", "бам", "патрокл", "остров русский", "тихая"]
    nearest_branches:
      "вторая речка": ["Гоголя"]
      "седанка": ["Гоголя"]
      "баляева": ["Черемуховая"]
      (fill in reasonable nearest for each area)

File 2: knowledge/base.py — Update validation:
  - Validate branches: each must have id, name, crm_branch_id, address, aliases
  - Validate aliases are all lowercase
  - Validate no alias appears in BOTH branches and unknown_areas (conflict = startup error)
  - Validate style_aliases: each has crm_style_id and non-empty aliases
  - Add accessor: get_branch_aliases() → dict
  - Add accessor: get_style_aliases() → dict
  - Add accessor: get_unknown_areas() → dict
  - IMPORTANT: If old studio.branches section exists and differs from new branches section,
    remove the old one. There must be ONE source of truth.

File 3: tests/unit/test_kb_aliases.py — Tests:
  - Validation passes with correct data
  - Validation fails: branch alias in unknown_areas → error
  - Validation fails: missing crm_branch_id → error
  - Validation fails: uppercase alias → error
  - get_branch_aliases returns correct mapping
  - get_style_aliases returns correct mapping

Do NOT modify services, teachers, faq, holidays, escalation sections.
Do NOT remove existing KB accessors.
```

### Промпт 4.2.4 — AliasEntityResolver (объединение)
```
Read RFC_004_ENTITY_RESOLVER.md section 4.6 ("Единая точка входа").

Task: Create AliasEntityResolver that implements the EntityResolver Protocol
by combining TeacherResolver, BranchResolver, and StyleResolver.

File 1: app/core/entity_resolver/alias_resolver.py — NEW file, under 80 lines:

  class AliasEntityResolver:
      """Implements EntityResolver Protocol. 
      Combines three resolvers. Single-tenant implementation."""
      
      def __init__(self, teacher_resolver: TeacherResolver,
                   branch_resolver: BranchResolver,
                   style_resolver: StyleResolver)
      
      async def resolve_teacher(self, raw: str, tenant_id: str) -> list[ResolvedEntity]:
          return self._teacher.resolve(raw, tenant_id)
      
      async def resolve_branch(self, raw: str, tenant_id: str) -> list[ResolvedEntity]:
          return self._branch.resolve(raw, tenant_id)
      
      async def resolve_style(self, raw: str, tenant_id: str) -> list[ResolvedEntity]:
          return self._style.resolve(raw, tenant_id)
      
      async def check_unknown_area(self, raw: str, tenant_id: str) -> str | None:
          return self._branch.check_unknown_area(raw, tenant_id)
      
      @property
      def is_ready(self) -> bool:
          """True if teacher sync completed."""
          return self._teacher.is_synced

  This is deliberately thin — just delegation. 
  No business logic here. That's in engine.py.

File 2: app/core/entity_resolver/__init__.py — Update exports:
  - EntityResolver (Protocol)
  - AliasEntityResolver (implementation)
  - ResolvedEntity, ResolvedEntities
  - TeacherResolver, BranchResolver, StyleResolver

File 3: tests/unit/test_entity_resolver.py — Integration tests:

  Setup: Mock CRM, mock KB with aliases
  
  test_resolve_teacher_delegates():
    resolver.resolve_teacher("настя", "t1") → delegates to teacher_resolver
  
  test_resolve_branch_delegates():
    resolver.resolve_branch("гоголя", "t1") → delegates to branch_resolver
  
  test_is_ready_false_before_sync():
    assert resolver.is_ready == False
  
  test_is_ready_true_after_sync():
    await resolver._teacher.sync(mock_crm)
    assert resolver.is_ready == True

Do NOT modify any other files.
```

---

## Phase 4.3: Интеграция с Engine

> ⚠️ Phase 4.3 ТРЕБУЕТ завершения RFC-003 (engine.py должен существовать).
> Если RFC-003 не готов — остановись после Phase 4.2.

### Промпт 4.3.1 — LLM slot_updates: добавить *_raw поля
```
Read RFC_004_ENTITY_RESOLVER.md sections 3.1 and 5.3.

Task: Update the LLM system prompt to extract teacher_raw, style_raw, branch_raw 
in slot_updates. Update LLMResponse model.

Step 1 — PLAN ONLY:
- Show how you will modify the prompt to instruct LLM to extract *_raw values
- Show the updated LLMResponse.slot_updates expected format
- Confirm: LLM extracts raw text as-is (e.g. "настюше", not "Анастасия")

Step 2 — After approval:

File 1: app/core/prompt_builder.py — Update system prompt:
  
  Add to the slot extraction instructions in the prompt:
  
  """
  When the user mentions a teacher name, style, or branch — extract the RAW text:
  - teacher_raw: the teacher name exactly as user wrote it (e.g. "настюше", "катя", "николаевой")
  - style_raw: the style/direction exactly as user wrote it (e.g. "каблуки", "гёрли", "хилс")
  - branch_raw: the branch/location exactly as user wrote it (e.g. "гоголя", "центр", "первая речка")
  
  Do NOT normalize these values. Do NOT translate "настя" to "Анастасия".
  Extract the EXACT words the user used.
  
  Example:
  User: "хочу к Настюше на каблуки на Гоголя"
  slot_updates: {"teacher_raw": "настюше", "style_raw": "каблуки", "branch_raw": "гоголя"}
  
  Example:
  User: "запишите на гёрли"
  slot_updates: {"style_raw": "гёрли"}
  
  Example:
  User: "хочу на занятие завтра вечером"
  slot_updates: {"datetime_raw": "завтра вечером"}
  (no teacher_raw, style_raw, or branch_raw — user didn't mention them)
  """

File 2: app/core/prompt_builder.py — Update LLMResponse:
  Add to intent enum:
    "continue" | "booking" | "cancel" | "escalate" | "info" 
    | "buy_subscription" | "ask_price" | "ask_trial"

Do NOT modify engine.py yet (next prompt).
Do NOT modify resolver files.

Run existing prompt regression tests — they should still pass ≥ 90%.
```

### Промпт 4.3.2 — Интеграция EntityResolver в engine.py
```
Read RFC_004_ENTITY_RESOLVER.md sections 5.1, 5.2, and 5.4.

Task: Wire EntityResolver into the ConversationEngine.
After LLM extracts slot_updates, resolve *_raw values to CRM IDs before CRM calls.

Step 1 — PLAN ONLY:
- Show where in handle_message() the resolver is called
- Show the _resolve_and_update_slots method signature
- Show how CRM calls change from text names to CRM IDs
- Confirm: resolver is called AFTER LLM extraction, BEFORE CRM calls
- Confirm: if *_raw is empty/None → resolver NOT called for that entity
- Confirm: if resolver returns >1 result → return clarification question, skip CRM call

Step 2 — After approval:

File 1: app/core/engine.py — Add resolver integration:

  class ConversationEngine:
      def __init__(self, ..., resolver: EntityResolver):
          self._resolver = resolver
      
      In handle_message(), AFTER LLM response and slot_updates extraction:
      
      # [NEW STEP] Resolve raw values to CRM IDs
      slot_updates = llm_response.slot_updates
      clarification = await self._resolve_and_update_slots(slot_updates, session.slots)
      if clarification:
          # Ambiguous or not found — return clarification question
          return clarification  # Goes to outbound queue
      
      # Continue with existing flow (CRM calls, guardrails, etc.)

  Add method per RFC-004 §5.2:
  
  async def _resolve_and_update_slots(self, slot_updates: dict, slots: SlotValues) -> str | None:
      """
      For each *_raw value in slot_updates:
        1. Call appropriate resolver
        2. If 1 result → update slots (canonical name + CRM ID)
        3. If >1 results → return clarification question
        4. If 0 results + is unknown area → return nearest branches message
        5. If 0 results → return "not found" message
      Returns None if all resolved successfully.
      """
      
      # IMPORTANT: Only resolve fields that are present in this update
      teacher_raw = slot_updates.get("teacher_raw")
      style_raw = slot_updates.get("style_raw")
      branch_raw = slot_updates.get("branch_raw")
      
      if teacher_raw:
          teachers = await self._resolver.resolve_teacher(teacher_raw, self._tenant_id)
          if len(teachers) > 1:
              names = [t.name for t in teachers]
              return f"У нас несколько преподавателей: {', '.join(names)}. К кому записать?"
          elif len(teachers) == 1:
              slots.teacher = teachers[0].name
              slots.teacher_id = teachers[0].crm_id
              slots.teacher_raw = teacher_raw
          else:
              return f"Не нашла преподавателя «{teacher_raw}». Подсказать, кто ведёт занятия?"
      
      # Same pattern for style_raw and branch_raw...
      # For branch: also check_unknown_area if resolve returned empty
      
      return None

File 2: app/core/schedule_flow.py — Update CRM calls:
  
  CHANGE from text-based filtering:
    schedules = [s for s in crm_data if s.branch_name == slots.branch]
  
  TO ID-based filtering:
    - If CRM supports filtering by IDs (columns parameter): pass teacher_id, style_id, branch_id
    - If CRM doesn't support (OQ-10 not resolved): filter on bot side by CRM IDs, not text names
  
  IMPORTANT: Keep the existing function signatures. Only change the filtering logic inside.

File 3: app/main.py — Initialize resolver at startup:
  
  On startup:
    1. Load names_dict.json
    2. Create TeacherResolver, BranchResolver, StyleResolver
    3. Create AliasEntityResolver
    4. Sync teachers from CRM: await teacher_resolver.sync(crm_adapter)
    5. If sync fails: log ERROR, alert admin, set degraded mode
       - Booking requests → "Технические неполадки, администратор свяжется с вами"
       - Info requests from KB → work normally
    6. Schedule periodic resync every 6 hours
    7. Pass resolver to ConversationEngine

Do NOT modify cancel_flow.py, idempotency.py, temporal.py, budget_guard.py.
```

### Промпт 4.3.3 — Новые intents: buy_subscription, ask_price, ask_trial
```
Read RFC_004_ENTITY_RESOLVER.md section 6 ("Разделение intents").

Task: Add new intents and route them in engine.py.

File 1: app/core/engine.py — Add intent routing:

  In handle_message(), after getting LLM response intent:

  if intent == "buy_subscription":
      # Answer about subscriptions from KB
      # Do NOT ask about direction/branch/date — these are booking questions
      # If user wants to actually pay → escalate to admin
      return self._handle_subscription_inquiry(session, llm_response)
  
  elif intent == "ask_price":
      # Answer about prices from KB  
      # Ask: групповые или индивидуальные?
      # Do NOT redirect to booking flow
      return self._handle_price_inquiry(session, llm_response)
  
  elif intent == "ask_trial":
      # Answer about trial lesson from KB (conditions, what to bring)
      # If user wants to book trial → redirect to booking flow with trial flag
      return self._handle_trial_inquiry(session, llm_response)

  These methods should:
  1. Look up relevant info from KB
  2. Let LLM compose the answer using KB data
  3. NOT ask booking-specific questions (direction, date, time)
  4. If user explicitly asks to book → switch to booking intent

File 2: app/core/guardrails.py — Add Guardrail G13:

  def check_intent_context(slots: SlotValues, response: LLMResponse) -> GuardrailResult:
      """
      If current intent is buy_subscription or ask_price,
      LLM should NOT be asking about direction/branch/datetime.
      Those questions belong to booking intent only.
      """

File 3: app/core/prompt_builder.py — Update prompt:
  
  Add to system prompt:
  """
  INTENT RULES:
  - "buy_subscription": User wants to BUY a subscription. Answer about types and prices.
    Do NOT ask about specific class dates or directions. If they want to pay, say 
    "Для покупки абонемента свяжитесь с администратором" and escalate.
  - "ask_price": User is asking about prices. Give price info from KB.
    Ask if they mean group or individual. Do NOT start booking flow.
  - "ask_trial": User asks about trial class. Explain conditions from KB.
    If they want to sign up for trial → switch to intent "booking".
  """

Run prompt regression tests. All existing tests must pass ≥ 90%.
```

### Промпт 4.3.4 — Единый branches source + prompt_builder fix
```
Read RFC_004_ENTITY_RESOLVER.md section 7 ("Единый источник филиалов").

Task: Fix the single-source-of-truth for branches. 
prompt_builder must read from the same branches as EntityResolver.

File 1: knowledge/base.py — Verify single source:

  @property
  def branches(self) -> list[dict]:
      """Single source of truth for branches. 
      Used by prompt_builder AND EntityResolver."""
      return self._data["branches"]

  If there is an old accessor that reads from studio.branches (different path):
    - Remove it or redirect to self.branches
    - There must be ONE list of branches, not two

File 2: app/core/prompt_builder.py — Update _format_kb_context:

  The "Наши филиалы" section in the prompt MUST be built from self._kb.branches
  (the same source EntityResolver uses).

  def _format_kb_context(self, slots, phase):
      branches = self._kb.branches
      branch_text = "\n".join(f"- {b['name']}: {b['address']}" for b in branches)
      ...

  Verify: if "Тест" is in branches config, bot will mention it.
  Verify: branch names match what EntityResolver returns.

This is a small change. Run prompt regression after.
```

---

## Phase 4.4: Тесты + Smoke

### Промпт 4.4.1 — Prompt regression тесты для entity resolution
```
Read RFC_004_ENTITY_RESOLVER.md section 10.2 ("Prompt regression — новые тесты").

Task: Add new prompt regression test suite for entity resolution.

File 1: tests/prompt_regression/test_entity_resolution.yaml — NEW suite:

- name: "diminutive_teacher_name"
  messages:
    - user: "Хочу к Насте на хилс"
  expected:
    not_contains: ["не нашлось", "не найден", "нет преподавателя"]
    contains_one_of: ["Анастасия", "расписание", "записать", "филиал"]

- name: "colloquial_branch_name"
  messages:
    - user: "На Первой речке есть занятия?"
  expected:
    contains_one_of: ["Гоголя", "Красного Знамени", "расписание", "направлен"]

- name: "center_disambiguation"
  messages:
    - user: "Хочу в центре заниматься"
  expected:
    contains: ["Алеутская"]
    contains: ["Семёновская"]
    contains_one_of: ["какой", "удобнее", "выбрать", "филиал"]

- name: "colloquial_style_name"
  messages:
    - user: "Запишите на гёрли"
  expected:
    not_contains: ["не найден", "нет такого", "не нашла"]
    contains_one_of: ["Girly", "филиал", "расписание"]

- name: "unknown_area"
  messages:
    - user: "Есть что-нибудь на Седанке?"
  expected:
    contains_one_of: ["нет филиала", "ближайш", "нет в этом районе"]

- name: "buy_subscription_not_booking"
  messages:
    - user: "Хочу купить абонемент на 8 занятий"
  expected:
    not_contains: ["направление", "какой стиль", "на какой день"]
    contains_one_of: ["абонемент", "стоимость", "цен"]

- name: "ask_price_not_booking"
  messages:
    - user: "Сколько стоит абонемент?"
  expected:
    not_contains: ["на какой день", "когда удобно"]
    contains_one_of: ["цен", "стоимость", "абонемент", "руб"]

Run: python -m tests.prompt_regression.runner
All existing tests must pass ≥ 90%.
New suite: pass ≥ 5/7 on first try. Tune prompt if needed.
```

### Промпт 4.4.2 — E2E smoke test с entity resolution
```
Task: Create smoke test that verifies entity resolution works end-to-end.

File 1: tests/e2e/test_entity_resolution_smoke.py — NEW file:

  Use TEST_MODE=true with mock CRM.

  Test 1 — Diminutive name booking:
    Simulate messages:
    1. "Привет! Хочу к Настюше на каблуки" 
       → should resolve to Анастасия + High Heels
       → should ask about branch (not "не нашла")
    2. "На Первой речке"
       → should resolve to Гоголя
       → should show schedule
    3-6. Complete booking flow
    
    Verify:
    - No "не найден" / "не нашлось" messages
    - CRM calls use teacher_id and style_id, not text names
    - Receipt contains correct teacher name and branch address

  Test 2 — Center disambiguation:
    1. "Хочу на хилс в центре"
       → should return both Алеутская and Семёновская
    2. "Алеутская"
       → should proceed with single branch

  Test 3 — Unknown area:
    1. "Есть занятия на Седанке?"
       → should mention "нет филиала" and suggest nearest

  Test 4 — Subscription intent:
    1. "Хочу купить абонемент на 8 занятий"
       → should NOT ask about direction/date
       → should give price info

  Test 5 — Teacher sync degraded:
    # Simulate sync failure
    → booking request should return "Технические неполадки"
    → price question should still work from KB

Run with: python -m pytest tests/e2e/test_entity_resolution_smoke.py -v
```

---

## Если Cursor делает что-то не то

### Cursor пытается парсить текст в resolver
```
STOP. EntityResolver does NOT parse user text.
Read RFC-004 §3.1: "LLM извлекает, Resolver нормализует".

The resolver receives PRE-EXTRACTED values from LLM slot_updates.
Each value is a single entity (one teacher name, one branch name, one style name).
There is NO tokenization, NO n-grams, NO text parsing in the resolver.

If you need to extract entities from text — that's LLM's job in prompt_builder.
```

### Cursor добавляет fuzzy matching
```
STOP. Fuzzy matching is OFF in MVP.
Read RFC-004 §4.1: "❌ Fuzzy matching ОТКЛЮЧЁН в MVP."

The algorithm is: exact match → pymorphy3 normal_form → not found.
No rapidfuzz, no fuzzywuzzy, no Levenshtein distance.
This will be added in Phase 4.2+ if real data shows it's needed.

Remove the fuzzy matching code.
```

### Cursor вызывает pymorphy3 перед exact match
```
STOP. Exact match MUST be checked FIRST.
Read RFC-004 §4.1: "Exact match first, pymorphy3 second."

Correct order:
1. normalized = raw.strip().lower()
2. if normalized in self._lookup: return    # O(1), no pymorphy3
3. ONLY THEN: lemma = pymorphy3.parse(normalized)[0].normal_form

pymorphy3 is a FALLBACK for case forms only. Not the primary lookup.
Fix the order.
```

### Cursor создаёт resolve_all метод
```
STOP. There is no resolve_all method.
Read RFC-004 §4.6: "Метод resolve_all удалён."

Engine.py calls each resolver separately:
  resolve_teacher(teacher_raw, tenant_id)
  resolve_style(style_raw, tenant_id)  
  resolve_branch(branch_raw, tenant_id)

Each receives ONE pre-extracted value, not the full user text.
Remove resolve_all and fix the integration.
```

### Cursor трогает cancel_flow.py или schedule_flow.py
```
STOP. Do NOT modify cancel_flow.py.
schedule_flow.py changes are LIMITED to:
  - Replacing text-based filtering with CRM ID filtering
  - Keeping existing function signatures

Read RFC-004 §8.2 for the exact list of files that should change.
Revert changes to cancel_flow.py.
```

### Cursor делает LLM-вызов внутри resolver
```
STOP. EntityResolver is DETERMINISTIC CODE. No LLM calls.
Read RFC-004 §9: "EntityResolver — код, не LLM. Детерминированный."
Read RFC-004 §3.1: "Resolver отвечает за нормализацию — детерминированно, без LLM"

The resolver uses:
- Dict lookups (O(1))
- pymorphy3 (deterministic morphology)
- Static names_dict.json

No API calls to LLM providers. No token costs. No latency variance.
Remove the LLM call from the resolver.
```

---

## Порядок выполнения (чеклист)

```
□ 0.1  Обновить cursorrules

Phase 4.1: Фундамент
□ 4.1.1 Модели данных + Protocol + SlotValues extension
□ 4.1.2 names_dict.json (словарь ~300 имён)
□ 4.1.3 TeacherResolver + CRM sync + unit tests

Phase 4.2: Resolvers + KB  
□ 4.2.1 BranchResolver + unit tests
□ 4.2.2 StyleResolver + unit tests
□ 4.2.3 studio.yaml aliases + KB validation + single branches source
□ 4.2.4 AliasEntityResolver + integration tests

Phase 4.3: Интеграция с Engine (ТРЕБУЕТ RFC-003)
□ 4.3.1 LLM slot_updates: *_raw поля в prompt
□ 4.3.2 EntityResolver в engine.py + schedule_flow CRM IDs + startup init
□ 4.3.3 Новые intents: buy_subscription, ask_price, ask_trial
□ 4.3.4 Единый branches source в prompt_builder

Phase 4.4: Тесты
□ 4.4.1 Prompt regression (7 новых тестов)
□ 4.4.2 E2E smoke test

Критерий готовности каждого шага:
- Unit тесты проходят
- docker compose up работает
- /health возвращает OK
- Prompt regression ≥ 90% (проверять после 4.3.1+)
```

---

## Зависимости между промптами

```
4.1.1 (models) ──► 4.1.3 (teacher) ──► 4.2.4 (combined)
                                              │
4.1.2 (names_dict) ──► 4.1.3                 │
                                              ▼
4.2.1 (branch) ──► 4.2.4 (combined) ──► 4.3.2 (engine integration)
4.2.2 (style)  ──► 4.2.4                     │
4.2.3 (yaml)   ──► 4.2.1, 4.2.2              ▼
                                         4.3.1 (prompt *_raw)
                                              │
                                              ▼
                                         4.3.3 (intents)
                                         4.3.4 (branches fix)
                                              │
                                              ▼
                                         4.4.1 (regression tests)
                                         4.4.2 (smoke test)
```
