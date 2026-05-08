# Архитектурный скетч MCP-сервера памяти для Obsidian Vault

Дата: 2026-05-08  
Статус: **approval draft v0.2 / prestart sketch**  
GRACE-статус: **не финализировано; требуется approval перед генерацией `docs/*.xml` артефактов**

> Этот документ является скорректированной архитектурной версией драфта по результатам согласованного анализа. Он фиксирует решения, модульные границы, DAG-зависимости, policy model, mutation pipeline, audit/index подход и MVP-boundaries. После approval его можно разложить на `docs/requirements.xml`, `docs/technology.xml`, `docs/development-plan.xml`, `docs/verification-plan.xml`, `docs/knowledge-graph.xml`.

---

## 1. Цель

Создать личный MCP-сервер памяти на VPS, который даёт агентам — Codex, Claude Code, OpenClaw и другим MCP-клиентам — безопасный доступ к Obsidian Vault.

Vault остаётся главным источником истины. Индексы, embeddings, audit-проекции и wiki-страницы являются производными слоями.

Главные качества системы:

```text
- безопасный доступ к Vault;
- контролируемая запись агентами;
- защита от перезаписи;
- поддержка Obsidian Sync;
- быстрый поиск;
- расширяемая semantic retrieval архитектура;
- traceable audit;
- GRACE-ready структура контрактов.
```

Главная архитектурная формула:

```text
Vault is truth.
Index is derivative.
Writes are planned.
Patches are constrained.
Revision is content hash.
Audit is machine-readable.
LOG.md is projection.
Search starts with FTS.
Semantic search comes later.
Wiki is propose-only.
Agents never delete.
```

---

## 2. Зафиксированные архитектурные решения

### ADR-001 — MCP transport

**Решение:** использовать `Streamable HTTP` через Tailscale/WireGuard.

```text
Agent Client
  -> Tailscale/WireGuard private network
  -> Streamable HTTP MCP endpoint
  -> Memory MCP Server
```

**Rationale:** MCP-сервер не должен торчать в публичный интернет. Приватная сеть снижает attack surface, но оставляет удобный HTTP transport для нескольких клиентов и агентов.

---

### ADR-002 — Vault storage и external sync

**Решение:** Vault синхронизируется через Obsidian Sync; MCP-сервер не является единственным писателем файлов.

```text
external_sync=true
```

**Rationale:** Obsidian Sync остаётся выбранным механизмом синхронизации между устройствами. Поэтому архитектура обязана учитывать внешние изменения и не может полагаться только на локальные lock-и MCP-сервера.

---

### ADR-003 — Source of truth

**Решение:** Markdown-файлы Vault являются source of truth; индекс, векторы, audit-витрины и wiki-проекции являются производными.

**Rationale:** Vault должен оставаться пригодным к чтению и восстановлению без MCP-сервера. Индексы могут быть пересозданы, а Markdown-файлы должны сохранять смысловую автономность.

---

### ADR-004 — Revision model

**Решение:** `revision = sha256(content)`.

```text
revision:
  source: file content
  algorithm: sha256
  purpose: optimistic concurrency

mtime:
  purpose: index refresh hint

size:
  purpose: metadata/index optimization
```

**Rationale:** `mtime` и `size` недостаточно надёжны для optimistic concurrency, особенно при external sync. Content hash является точной защитой от stale write и silent overwrite.

---

### ADR-005 — Mutation model

**Решение:** разрешить ограниченный набор mutation tools:

```text
create_note
append_note
edit_note with constrained patches
write_note for trusted writers
```

**Rationale:** `create_note` и `append_note` покрывают безопасные сценарии накопления памяти. `edit_note` и `write_note` нужны для управляемого обслуживания Vault, но требуют усиленного pipeline из-за риска перезаписи.

Минимальные требования для `edit_note`:

```text
- expected_revision обязателен;
- dry_run обязателен;
- patch применяется только к текущей revision;
- результат patch проходит post-apply validation;
- mutation result возвращает diff;
- при малейшем конфликте операция отклоняется.
```

---

### ADR-006 — Constrained patch policy для `edit_note`

**Решение:** MVP не использует свободный regex/LLM patch. Разрешены управляемые patch-операции:

```text
replace_exact_block
append_under_heading
replace_section
insert_after_heading
replace_exact_text
```

**Rationale:** свободные regex/LLM-patches трудно предсказуемы и плохо проверяемы. Constrained patch format делает dry-run, validation и diff стабильными для автономных агентов.

---

### ADR-007 — `write_note`

**Решение:** `write_note` разрешён только trusted writer/trusted admin и является high-risk operation.

Условия:

```text
- только trusted_writer или trusted_admin;
- expected_revision обязателен;
- dry_run обязателен;
- diff обязателен;
- max file size limit;
- запрет записи в denylisted paths;
- запрет перезаписи файла, если revision изменилась;
- audit event обязателен.
```

**Rationale:** full-file replacement может уничтожить содержимое заметки. Поэтому операция допускается только при строгой идентичности expected/current revision и полной traceability.

---

### ADR-008 — Rename / move

**Решение:** разрешить только единичный move/promote для trusted profile + dry-run.

Запрещены:

```text
- delete;
- bulk move;
- bulk rename;
- overwrite existing target;
- move из/в denylisted folders.
```

Разрешённые безопасные сценарии:

```text
move_note_once
promote_note
```

Рекомендуемый `promote_note` contract:

```text
from: inbox/ или drafts/
to: memory/ или summaries/
requires: dry_run + trusted profile
overwrite: false
```

**Rationale:** rename/move может разрушить Obsidian links и структуру Vault. Единичный promote/move проще проверить и аудитировать, чем общий `move_file`.

---

### ADR-009 — No delete operations

**Решение:** delete operations не входят в MVP и запрещены по умолчанию.

**Rationale:** удаление заметок является необратимой high-risk операцией для личной памяти. Для MVP безопаснее сохранять все изменения как append/edit/move без удаления.

---

### ADR-010 — Audit log

**Решение:** machine-readable audit является authoritative; `LOG.md` является human-readable projection.

```text
authoritative audit:
  SQLite audit_log table или NDJSON append-only log

human projection:
  LOG.md
```

**Rationale:** Markdown-журнал удобен в Obsidian, но уязвим к гонкам, ручным правкам и повреждению структуры. Машиночитаемый append-only audit надёжнее для traceability и verification.

---

### ADR-011 — Index refresh coupling

**Решение:** mutation возвращает `affected_paths`; refresh вызывает `M-001 MCPServerEntrypoint` или background worker.

Запрещено:

```text
M-008 VaultMutationService -> M-014 IndexRefreshService
```

Разрешено:

```text
M-008 VaultMutationService
  -> returns affected_paths

M-001 MCPServerEntrypoint / BackgroundRefreshWorker
  -> M-014 IndexRefreshService.refresh_paths(affected_paths)
```

**Rationale:** прямой вызов index refresh из mutation service создаёт циклическую связность и усложняет отладку. `affected_paths` отделяет write pipeline от derivative index update.

Mutation result shape:

```json
{
  "status": "success",
  "path": "memory/example.md",
  "old_revision": "sha256:...",
  "new_revision": "sha256:...",
  "affected_paths": ["memory/example.md"],
  "audit_event_id": "EVT-...",
  "dry_run": false
}
```

---

### ADR-012 — Search index

**Решение:** MVP использует SQLite FTS5.

```text
MVP:
  SQLite FTS5

After MVP:
  SQLite FTS5 + sqlite-vec
```

**Rationale:** SQLite FTS5 даёт быстрый локальный full-text search без внешней инфраструктуры. Для личного Vault это надёжнее и проще Qdrant на старте.

---

### ADR-013 — Semantic backend

**Решение:** модуль должен называться `VectorSearchAdapter`, а не `QdrantAdapter`.

Реализации:

```text
disabled
sqlite_vec
qdrant_optional
```

**Rationale:** архитектурная граница должна описывать capability, а не конкретный backend. `sqlite-vec` выбран как preferred future backend; Qdrant остаётся будущим adapter для большого Vault.

---

### ADR-014 — SQLite mode

**Решение:** использовать WAL + single writer queue.

```text
SQLite journal_mode=WAL
SQLite busy_timeout
single writer queue для index update
multiple readers для search
```

**Rationale:** WAL повышает устойчивость одновременного чтения/записи, а single writer queue снижает риск `database is locked` при индексировании.

---

### ADR-015 — Lock model

**Решение:** гибридный lock.

```text
process-level lock:
  asyncio.Lock per normalized path

filesystem-level lock:
  lock file / file lock for critical mutations
```

**Rationale:** process-level lock защищает от конкурентных requests внутри одного процесса. Filesystem-level lock нужен на случай второго процесса сервера или внешней автоматизации на том же VPS.

---

### ADR-016 — Symlink policy

**Решение:** запретить все symlink-и в MVP.

```text
If path or any parent component is symlink:
  deny
```

**Rationale:** symlink-и усложняют sandbox boundary и могут привести к чтению/записи вне Vault root. Полный запрет проще проверить и безопаснее для MVP.

---

### ADR-017 — Sandbox and drafts

**Решение:** архитектура поддерживает `drafts/ + promote_note`, но MVP может отключить `drafts/` initially.

```text
Architecture supports drafts/promote.
MVP may disable drafts folder initially.
```

Будущая модель:

```text
drafts/
  агент может создавать черновики

promote_note:
  переносит утверждённую заметку в memory/
```

**Rationale:** drafts/promote полезен для staged writing, но не должен блокировать MVP. Контракт закладывается сейчас, включение папки можно сделать позже policy-флагом.

---

### ADR-018 — Wiki automation

**Решение:** wiki automation работает в propose-only режиме и генерирует diff для `70_Wiki`.

Разрешено:

```text
find_wiki_sources
propose_wiki_update
generate_wiki_diff
```

Запрещено по умолчанию:

```text
write directly to 70_Wiki
```

**Rationale:** wiki-страницы являются синтетическим знанием и легко портятся автономными правками. Propose-only сохраняет traceability и требует review перед применением.

---

### ADR-019 — Rollback

**Решение:** rollback в MVP выполняется вручную через Git backup; Rollback API не входит в MVP.

Будущие tools:

```text
get_note_history
restore_note_revision
```

**Rationale:** Git уже является надёжным механизмом истории Markdown-файлов. Специальный rollback API расширяет attack surface и требует отдельной verification модели.

---

### ADR-020 — Write folders

**Решение:** MVP write allowlist:

```text
inbox/
memory/
summaries/
LOG.md projection
```

Особые правила:

```text
70_Wiki:
  propose-first only

drafts/:
  supported by architecture
  can be enabled later

private/
secrets/
.obsidian/
.git/:
  denylist
```

**Rationale:** allowlist минимизирует риск случайной порчи Vault. `70_Wiki` и sensitive folders имеют отдельные правила из-за повышенной ценности или секретности.

---

## 3. Итоговая модульная структура

| ID | Name | Type | Purpose | Dependencies | Target paths | Verification ref |
|---|---|---|---|---|---|---|
| `M-001` | `MCPServerEntrypoint` | `ENTRY_POINT` | Exposes Streamable HTTP MCP tools and coordinates auth, policy, mutation, retrieval, index refresh and health endpoints. | `M-002`, `M-003`, `M-004`, `M-008`, `M-010`, `M-014`, `M-017`, `M-018`, `M-019`, `M-012` | `src/memory_mcp/server.py` | `V-M-001` |
| `M-002` | `AuthIdentityService` | `CORE_LOGIC` | Resolves agent identity, API key, role and policy profile. | `M-011`, `M-012` | `src/memory_mcp/auth.py` | `V-M-002` |
| `M-003` | `VaultPolicyGuard` | `CORE_LOGIC` | Enforces allowlist, denylist, no-delete, no-bulk-move, dry-run, propose-only and symlink-deny policies. | `M-002`, `M-011`, `M-012` | `src/memory_mcp/policy.py` | `V-M-003` |
| `M-004` | `VaultReadService` | `CORE_LOGIC` | Provides safe read/list/exists/metadata operations over allowed Vault paths. | `M-003`, `M-005`, `M-012` | `src/memory_mcp/read_service.py` | `V-M-004` |
| `M-005` | `VaultFilesystemRepository` | `DATA_LAYER` | Performs normalized path resolution, symlink denial, file reads, metadata inspection and atomic writes. | `M-011`, `M-012` | `src/memory_mcp/vault_fs.py` | `V-M-005` |
| `M-006` | `ChangePlanner` | `CORE_LOGIC` | Builds dry-run diffs, validates constrained patches and creates executable mutation plans. | `M-003`, `M-005`, `M-007`, `M-015`, `M-012` | `src/memory_mcp/change_planner.py` | `V-M-006` |
| `M-007` | `LockAndRevisionService` | `CORE_LOGIC` | Provides hybrid locks and `sha256(content)` optimistic concurrency checks. | `M-005`, `M-011`, `M-012` | `src/memory_mcp/concurrency.py` | `V-M-007` |
| `M-008` | `VaultMutationService` | `CORE_LOGIC` | Executes approved create/append/edit/write operations and returns `affected_paths` without calling index refresh directly. | `M-003`, `M-005`, `M-006`, `M-007`, `M-009`, `M-012` | `src/memory_mcp/mutation_service.py` | `V-M-008` |
| `M-009` | `AuditLogService` | `CORE_LOGIC` | Writes authoritative machine-readable audit events and updates `LOG.md` projection. | `M-005`, `M-007`, `M-011`, `M-012` | `src/memory_mcp/audit_log.py` | `V-M-009` |
| `M-010` | `RetrievalService` | `CORE_LOGIC` | Provides keyword, FTS, optional semantic, similar-notes, wiki-source retrieval and explainable ranking. | `M-003`, `M-013`, `M-015`, `M-016`, `M-012` | `src/memory_mcp/retrieval_service.py` | `V-M-010` |
| `M-011` | `ConfigService` | `UTILITY` | Loads server, Vault, auth, policy, index, audit and backup configuration. | none | `src/memory_mcp/config.py` | `V-M-011` |
| `M-012` | `ObservabilityService` | `UTILITY` | Provides structured logs, request IDs, trace anchors and error taxonomy. | `M-011` | `src/memory_mcp/observability.py` | `V-M-012` |
| `M-013` | `IndexRepository` | `DATA_LAYER` | Maintains SQLite metadata, FTS5, audit-index access and future vector tables derived from Vault files. | `M-005`, `M-011`, `M-012` | `src/memory_mcp/index_repo.py` | `V-M-013` |
| `M-014` | `IndexRefreshService` | `CORE_LOGIC` | Rebuilds or incrementally updates the index from Vault content and `affected_paths`. | `M-003`, `M-005`, `M-013`, `M-015`, `M-016`, `M-012` | `src/memory_mcp/index_refresh.py` | `V-M-014` |
| `M-015` | `MarkdownSemanticParser` | `UTILITY` | Extracts frontmatter, tags, links, headings, sections and validates post-apply Markdown shape. | `M-012` | `src/memory_mcp/markdown_parser.py` | `V-M-015` |
| `M-016` | `VectorSearchAdapter` | `INTEGRATION` | Provides optional vector search via disabled/sqlite-vec/qdrant-optional implementations. | `M-011`, `M-012` | `src/memory_mcp/vector_adapter.py` | `V-M-016` |
| `M-017` | `BackupService` | `INTEGRATION` | Exposes manual Git backup/health integration without implementing rollback API in MVP. | `M-005`, `M-011`, `M-012` | `src/memory_mcp/backup_service.py` | `V-M-017` |
| `M-018` | `WikiUpdateService` | `CORE_LOGIC` | Finds wiki sources and generates propose-only diffs for `70_Wiki`. | `M-003`, `M-006`, `M-010`, `M-015`, `M-012` | `src/memory_mcp/wiki_update_service.py` | `V-M-018` |
| `M-019` | `DraftPromotionService` | `CORE_LOGIC` | Controls single-note promote/move workflows from `inbox/` or future `drafts/` to approved destinations. | `M-003`, `M-005`, `M-006`, `M-007`, `M-009`, `M-012` | `src/memory_mcp/draft_promotion_service.py` | `V-M-019` |

**Rationale:** `M-019 DraftPromotionService` выделен отдельно, потому что `drafts/ -> memory/` и `inbox/ -> memory/` являются отдельным safe-move сценарием с более строгими ограничениями, чем обычные file mutation operations.

---

## 4. Dependency DAG

### 4.1 DAG-принцип

Ключевое правило:

```text
M-008 MUST NOT depend on M-014
```

Правильная связность:

```text
M-008 VaultMutationService
  -> returns affected_paths

M-001 MCPServerEntrypoint
  -> calls M-014 IndexRefreshService

or

BackgroundRefreshWorker
  -> calls M-014 IndexRefreshService
```

### 4.2 Text DAG

```text
M-011 ConfigService
  -> M-002 AuthIdentityService
  -> M-003 VaultPolicyGuard
  -> M-005 VaultFilesystemRepository
  -> M-007 LockAndRevisionService
  -> M-009 AuditLogService
  -> M-012 ObservabilityService
  -> M-013 IndexRepository
  -> M-016 VectorSearchAdapter
  -> M-017 BackupService

M-012 ObservabilityService
  -> all runtime modules as logging/trace dependency

M-002 AuthIdentityService
  -> M-003 VaultPolicyGuard

M-003 VaultPolicyGuard
  -> M-004 VaultReadService
  -> M-006 ChangePlanner
  -> M-008 VaultMutationService
  -> M-010 RetrievalService
  -> M-014 IndexRefreshService
  -> M-018 WikiUpdateService
  -> M-019 DraftPromotionService

M-005 VaultFilesystemRepository
  -> M-004 VaultReadService
  -> M-006 ChangePlanner
  -> M-007 LockAndRevisionService
  -> M-008 VaultMutationService
  -> M-009 AuditLogService
  -> M-013 IndexRepository
  -> M-014 IndexRefreshService
  -> M-017 BackupService
  -> M-019 DraftPromotionService

M-007 LockAndRevisionService
  -> M-006 ChangePlanner
  -> M-008 VaultMutationService
  -> M-009 AuditLogService
  -> M-019 DraftPromotionService

M-015 MarkdownSemanticParser
  -> M-006 ChangePlanner
  -> M-010 RetrievalService
  -> M-014 IndexRefreshService
  -> M-018 WikiUpdateService

M-013 IndexRepository
  -> M-010 RetrievalService
  -> M-014 IndexRefreshService

M-016 VectorSearchAdapter
  -> M-010 RetrievalService
  -> M-014 IndexRefreshService

M-006 ChangePlanner
  -> M-008 VaultMutationService
  -> M-018 WikiUpdateService
  -> M-019 DraftPromotionService

M-008 VaultMutationService
  -> M-001 MCPServerEntrypoint via mutation result only

M-010 RetrievalService
  -> M-001 MCPServerEntrypoint
  -> M-018 WikiUpdateService

M-014 IndexRefreshService
  -> M-001 MCPServerEntrypoint or BackgroundRefreshWorker

M-017 BackupService
  -> M-001 MCPServerEntrypoint

M-018 WikiUpdateService
  -> M-001 MCPServerEntrypoint

M-019 DraftPromotionService
  -> M-001 MCPServerEntrypoint
```

### 4.3 Circular dependency check

**Циклов нет**, если соблюдаются ограничения:

```text
- M-008 does not call M-014.
- M-018 generates propose-only diffs and does not call M-008 directly.
- M-019 handles only constrained single-note move/promote and does not call M-014 directly.
```

**Flagged circular dependency risk:**

```text
M-008 -> M-014 -> M-005 -> M-008
```

Эта связность запрещена. Единственное разрешённое сопряжение mutation/index — через `affected_paths` в результате mutation.

---

## 5. Основные MCP tools

### Read tools

```text
read_note
check_file_exists
list_allowed_paths
get_note_metadata
```

### Search tools

```text
search_notes
find_similar_notes
find_wiki_sources
explain_ranking
```

### Mutation tools

```text
create_note
append_note
edit_note
write_note
move_note_once
promote_note
```

### Planning tools

```text
prepare_change
preview_diff
validate_patch
```

### Wiki tools

```text
propose_wiki_update
generate_wiki_diff
```

### Index tools

```text
refresh_index
refresh_paths
refresh_wiki_folder
```

### Admin tools

```text
health_check
backup_vault
```

**Rationale:** tools разделены на read/search/mutation/planning/wiki/index/admin, чтобы policy можно было назначать по capability class, а не по случайным function names.

---

## 6. Mutation pipeline

Финальный pipeline:

```text
MCP Tool
  -> AuthIdentityService
  -> VaultPolicyGuard
  -> ChangePlanner
  -> Dry Run / Diff
  -> LockAndRevisionService
  -> External Sync Revision Recheck
  -> VaultFilesystemRepository atomic write
  -> AuditLogService authoritative event
  -> LOG.md projection update
  -> return affected_paths
  -> IndexRefreshService via entrypoint/background worker
```

**Rationale:** pipeline отделяет планирование от исполнения, гарантирует recheck перед записью и не связывает mutation напрямую с index refresh.

---

## 7. External Sync handling

Так как выбран Obsidian Sync, включается отдельный режим:

```xml
<external-sync id="ES-001" mode="obsidian_sync">
  <revision_model>sha256(content)</revision_model>
  <before_write>recompute_current_revision</before_write>
  <on_revision_mismatch>reject_with_conflict</on_revision_mismatch>
  <after_write>recompute_new_revision</after_write>
  <index_strategy>startup_reconcile + incremental_refresh</index_strategy>
</external-sync>
```

Поведение при конфликте:

```json
{
  "status": "conflict",
  "reason": "STALE_REVISION",
  "expected_revision": "sha256:old...",
  "current_revision": "sha256:new...",
  "message": "File was changed externally before mutation was applied."
}
```

**Rationale:** external sync означает, что файл может измениться между read и write даже при локальном lock. Поэтому перед записью нужен повторный content-hash recheck.

---

## 8. Policy model

### Agent profiles

```text
readonly_agent:
  read/search only
  propose_only=true

trusted_writer:
  read/search/create/append/edit/write
  requires_dry_run=true
  write roots: inbox, memory, summaries

wiki_maintainer:
  read/search
  propose_wiki_update
  70_Wiki write = propose-only

cron_summarizer:
  read/search
  append summaries
  write roots: summaries
  no full write

admin:
  all trusted operations except delete/bulk move
```

### Write policy

```xml
<policy-shape id="POL-001">
  <write_allowlist>
    <path>inbox/</path>
    <path>memory/</path>
    <path>summaries/</path>
    <path>LOG.md</path>
  </write_allowlist>
  <propose_only_paths>
    <path>70_Wiki/</path>
  </propose_only_paths>
  <future_write_paths disabled_by_default="true">
    <path>drafts/</path>
  </future_write_paths>
  <denylist>
    <path>private/</path>
    <path>secrets/</path>
    <path>.obsidian/</path>
    <path>.git/</path>
  </denylist>
  <delete_allowed>false</delete_allowed>
  <bulk_move_allowed>false</bulk_move_allowed>
  <symlinks_allowed>false</symlinks_allowed>
</policy-shape>
```

**Rationale:** profiles ограничивают damage radius при компрометации агента или ошибке prompt/tool call. Write allowlist и denylist должны применяться до любого filesystem access.

---

## 9. Patch policy для `edit_note`

Запрещено начинать со свободного regex patch.

Разрешённые операции:

```text
append_under_heading(path, heading, content)
replace_section(path, heading, new_content)
replace_exact_text(path, old_text, new_text)
replace_exact_block(path, old_block, new_block)
insert_after_heading(path, heading, content)
```

Обязательные условия:

```text
expected_revision required
dry_run required
diff returned
patch validation required
post-apply markdown validation required
```

```xml
<patch-policy id="PP-001">
  <allowed_operation id="PP-OP-001">append_under_heading</allowed_operation>
  <allowed_operation id="PP-OP-002">replace_section</allowed_operation>
  <allowed_operation id="PP-OP-003">replace_exact_text</allowed_operation>
  <allowed_operation id="PP-OP-004">replace_exact_block</allowed_operation>
  <allowed_operation id="PP-OP-005">insert_after_heading</allowed_operation>
  <denied_operation id="PP-DENY-001">free_form_regex_patch</denied_operation>
  <requirement id="PP-REQ-001">expected_revision</requirement>
  <requirement id="PP-REQ-002">dry_run</requirement>
  <requirement id="PP-REQ-003">diff</requirement>
  <requirement id="PP-REQ-004">post_apply_validation</requirement>
</patch-policy>
```

**Rationale:** ограниченный patch language делает изменения предсказуемыми, reviewable и тестируемыми.

---

## 10. Index architecture

Baseline tables:

```text
notes
notes_fts
links
sections
frontmatter
tags
audit_log
index_state
```

FTS baseline:

```text
notes_fts:
  title
  headings
  body
```

Semantic later:

```text
note_chunks_vec:
  path
  chunk_id
  heading_path
  content_hash
  embedding_model
  vector
```

SQLite mode:

```xml
<sqlite-index-mode id="IDX-001">
  <journal_mode>WAL</journal_mode>
  <busy_timeout>enabled</busy_timeout>
  <writer_model>single_writer_queue</writer_model>
  <reader_model>multiple_readers</reader_model>
  <baseline_backend>FTS5</baseline_backend>
  <future_vector_backend>sqlite_vec</future_vector_backend>
</sqlite-index-mode>
```

**Rationale:** index remains rebuildable from Vault, while FTS5 provides reliable MVP search and sqlite-vec leaves a local path to semantic retrieval.

---

## 11. Audit architecture

Authoritative audit:

```text
SQLite audit_log или append-only NDJSON
```

Event shape:

```json
{
  "event_id": "EVT-20260508-001",
  "timestamp": "2026-05-08T12:00:00+02:00",
  "agent_id": "claude-code",
  "operation": "edit_note",
  "path": "memory/example.md",
  "old_revision": "sha256:...",
  "new_revision": "sha256:...",
  "dry_run": false,
  "policy_profile": "trusted_writer",
  "result": "success",
  "affected_paths": ["memory/example.md"],
  "trace_id": "req-..."
}
```

`LOG.md` projection:

```markdown
## 2026-05-08T12:00:00+02:00 — edit_note

- event_id: EVT-20260508-001
- agent_id: claude-code
- path: memory/example.md
- result: success
```

**Rationale:** authoritative audit нужен для машинной проверки и восстановления причинно-следственных цепочек; `LOG.md` нужен только для удобного чтения человеком в Obsidian.

---

## 12. Data flow diagrams

### DF-001 — Safe read

```text
Agent MCP Client
  -> M-001 MCPServerEntrypoint
  -> M-002 AuthIdentityService
  -> M-003 VaultPolicyGuard
  -> M-004 VaultReadService
  -> M-005 VaultFilesystemRepository
  -> return content + revision + metadata
```

**Rationale:** read path тоже проходит policy, чтобы denylisted content не попадал в ответы и индексные подсказки.

---

### DF-002 — Safe mutation

```text
Agent MCP Client
  -> M-001 MCPServerEntrypoint
  -> M-002 AuthIdentityService
  -> M-003 VaultPolicyGuard
  -> M-006 ChangePlanner
       - validate constrained patch
       - produce dry-run diff
       - require expected_revision where needed
  -> M-007 LockAndRevisionService
       - acquire process/filesystem lock
       - recompute current sha256(content)
       - reject stale revision
  -> M-008 VaultMutationService
       - execute atomic write via M-005
  -> M-009 AuditLogService
       - authoritative audit event
       - LOG.md projection
  -> return mutation result + affected_paths
  -> M-001 or BackgroundRefreshWorker calls M-014.refresh_paths(affected_paths)
```

**Rationale:** this flow prevents stale writes, records audit, and keeps index refresh decoupled.

---

### DF-003 — Retrieval/search

```text
Agent MCP Client
  -> M-001 MCPServerEntrypoint
  -> M-002 AuthIdentityService
  -> M-003 VaultPolicyGuard
  -> M-010 RetrievalService
       - query M-013 SQLite FTS5
       - optionally query M-016 VectorSearchAdapter
       - re-check denied paths before response
  -> return snippets/sources/ranking explanation
```

**Rationale:** retrieval must never bypass policy even if stale index entries exist.

---

### DF-004 — Index refresh from mutation result

```text
Mutation result
  -> affected_paths
  -> M-001 MCPServerEntrypoint or BackgroundRefreshWorker
  -> M-014 IndexRefreshService.refresh_paths
  -> M-003 VaultPolicyGuard
  -> M-005 VaultFilesystemRepository
  -> M-015 MarkdownSemanticParser
  -> M-013 IndexRepository
  -> M-016 VectorSearchAdapter optional
```

**Rationale:** index refresh is derivative and asynchronous/supervised; mutation remains linear and safer.

---

### DF-005 — Wiki propose-only update

```text
Agent wants wiki update
  -> M-001 MCPServerEntrypoint
  -> M-010 RetrievalService.find_wiki_sources
  -> M-018 WikiUpdateService
       - generate proposed update
       - generate diff
  -> M-006 ChangePlanner preview
  -> return propose-only diff; no autonomous write to 70_Wiki
```

**Rationale:** wiki automation can synthesize incorrect knowledge, so output is a diff proposal, not a mutation.

---

### DF-006 — Draft/inbox promotion

```text
Trusted Agent
  -> M-001 MCPServerEntrypoint
  -> M-002 AuthIdentityService
  -> M-003 VaultPolicyGuard
  -> M-019 DraftPromotionService
       - validate from/to roots
       - require dry_run
       - reject overwrite target
       - reject denylisted paths
  -> M-007 LockAndRevisionService
  -> M-005 VaultFilesystemRepository atomic move/rename primitive
  -> M-009 AuditLogService
  -> return affected_paths for source and target
```

**Rationale:** promotion is a distinct lifecycle transition and should not be hidden inside unrestricted move/rename operations.

---

## 13. Verification surface overview

### Critical flows

| Flow ID | Name | Must verify |
|---|---|---|
| `DF-001` | Safe read | auth, policy filtering, symlink denial, revision metadata |
| `DF-002` | Safe mutation | dry-run, constrained patch, lock, external sync recheck, atomic write, audit, affected_paths |
| `DF-003` | Retrieval/search | denylist not leaked, FTS correctness, explainable ranking, stale index filtering |
| `DF-004` | Index refresh | mutation does not call M-014 directly, incremental refresh correctness, startup reconcile |
| `DF-005` | Wiki propose-only | source traceability, diff generation, no autonomous `70_Wiki` write |
| `DF-006` | Draft/inbox promotion | trusted profile, dry-run, no overwrite, no bulk move, audit |

### Module-local checks

| Verification ref | Checks |
|---|---|
| `V-M-001` | tool routing applies auth/policy before services; calls M-014 only after `affected_paths` result |
| `V-M-002` | valid identity accepted; invalid key rejected; profile resolved |
| `V-M-003` | allowlist/denylist/propose-only/dry-run/no-delete/no-bulk/symlink-deny enforced |
| `V-M-004` | read returns content + `sha256(content)` revision and never exposes denied paths |
| `V-M-005` | path traversal blocked; any symlink component denied; atomic write semantics |
| `V-M-006` | dry-run diff stable; constrained patch validation; post-apply validation |
| `V-M-007` | process and filesystem locks; stale revision rejected after external sync change |
| `V-M-008` | create/append/edit/write use same pipeline; returns affected_paths; never calls M-014 |
| `V-M-009` | authoritative audit event written; `LOG.md` projection generated; no recursive audit loop |
| `V-M-010` | search filters denied paths; FTS works without vector backend; ranking explainable |
| `V-M-011` | config loads transport, policy, audit, index and external sync settings |
| `V-M-012` | trace ids and error taxonomy present on critical flows |
| `V-M-013` | SQLite FTS5 schema and WAL mode; index state tracks revision/content hash |
| `V-M-014` | refresh_paths handles affected_paths; startup reconcile detects external changes |
| `V-M-015` | frontmatter/tags/headings/sections parsed; invalid markdown shapes rejected when required |
| `V-M-016` | disabled/sqlite_vec/qdrant_optional modes; vector failure degrades to FTS |
| `V-M-017` | manual Git backup does not mutate Vault content; excludes denylisted secrets if configured |
| `V-M-018` | `70_Wiki` update remains propose-only and produces source-linked diffs |
| `V-M-019` | promote/move is single-file only, trusted-only, dry-run required, no overwrite |

### Trace anchors

```xml
<trace-anchor id="TA-001" name="auth.identity.resolved" />
<trace-anchor id="TA-002" name="policy.decision" />
<trace-anchor id="TA-003" name="change.plan.created" />
<trace-anchor id="TA-004" name="patch.validation.completed" />
<trace-anchor id="TA-005" name="lock.acquired" />
<trace-anchor id="TA-006" name="revision.rechecked" />
<trace-anchor id="TA-007" name="vault.atomic_write.completed" />
<trace-anchor id="TA-008" name="audit.authoritative_event.appended" />
<trace-anchor id="TA-009" name="audit.log_projection.updated" />
<trace-anchor id="TA-010" name="mutation.affected_paths.returned" />
<trace-anchor id="TA-011" name="index.refresh.started" />
<trace-anchor id="TA-012" name="index.refresh.completed" />
<trace-anchor id="TA-013" name="retrieval.query.completed" />
<trace-anchor id="TA-014" name="retrieval.denied_path.filtered" />
<trace-anchor id="TA-015" name="wiki.diff.generated" />
<trace-anchor id="TA-016" name="backup.snapshot.completed" />
```

### Stop conditions / replan triggers

```xml
<stop-condition id="SC-001">
  <condition>Any write path bypasses M-003 VaultPolicyGuard or M-006 ChangePlanner.</condition>
  <action>Stop implementation and replan mutation boundary.</action>
</stop-condition>

<stop-condition id="SC-002">
  <condition>M-008 VaultMutationService directly calls M-014 IndexRefreshService.</condition>
  <action>Stop and restore affected_paths coupling.</action>
</stop-condition>

<stop-condition id="SC-003">
  <condition>Concurrent write or external sync test can silently overwrite a newer content hash.</condition>
  <action>Stop and redesign lock/revision behavior.</action>
</stop-condition>

<stop-condition id="SC-004">
  <condition>Any retrieval response exposes denylisted content or metadata.</condition>
  <action>Stop and redesign indexing/query response filtering.</action>
</stop-condition>

<stop-condition id="SC-005">
  <condition>Any symlink path or parent component is accepted in MVP.</condition>
  <action>Stop and tighten path resolution.</action>
</stop-condition>

<stop-condition id="SC-006">
  <condition>Wiki update mutates 70_Wiki without propose-only diff review.</condition>
  <action>Stop and tighten WikiUpdateService contract.</action>
</stop-condition>

<stop-condition id="SC-007">
  <condition>Index is treated as source of truth instead of rebuildable derivative.</condition>
  <action>Stop and reassert Vault-first architecture.</action>
</stop-condition>
```

---

## 14. Implementation order

### Phase 1 — Foundation and safety boundary

```xml
<Phase-1 id="PH-001" name="FoundationAndPolicy">
  <step-1 module="M-011">ConfigService</step-1>
  <step-2 module="M-012">ObservabilityService</step-2>
  <step-3 module="M-002">AuthIdentityService</step-3>
  <step-4 module="M-003">VaultPolicyGuard</step-4>
  <step-5 module="M-005">VaultFilesystemRepository</step-5>
</Phase-1>
```

**Rationale:** нельзя строить tools до auth, policy, path safety, symlink deny и observability.

---

### Phase 2 — Read-only MCP server

```xml
<Phase-2 id="PH-002" name="ReadOnlyMCP">
  <step-1 module="M-001">MCPServerEntrypoint read tools over Streamable HTTP</step-1>
  <step-2 module="M-004">VaultReadService</step-2>
</Phase-2>
```

**Rationale:** безопасное чтение проверяет transport, identity, policy and path resolution before mutations exist.

---

### Phase 3 — Safe mutation pipeline

```xml
<Phase-3 id="PH-003" name="SafeMutationPipeline">
  <step-1 module="M-007">LockAndRevisionService</step-1>
  <step-2 module="M-006">ChangePlanner</step-2>
  <step-3 module="M-009">AuditLogService</step-3>
  <step-4 module="M-008">VaultMutationService</step-4>
</Phase-3>
```

**Rationale:** write tools появляются только после dry-run, constrained patches, hybrid locking, external sync revision recheck and audit.

---

### Phase 4 — SQLite FTS retrieval and refresh

```xml
<Phase-4 id="PH-004" name="SQLiteFTSRetrieval">
  <step-1 module="M-015">MarkdownSemanticParser</step-1>
  <step-2 module="M-013">IndexRepository SQLite FTS5 + WAL</step-2>
  <step-3 module="M-014">IndexRefreshService with affected_paths</step-3>
  <step-4 module="M-010">RetrievalService keyword/FTS</step-4>
</Phase-4>
```

**Rationale:** MVP search должен быть reliable без внешних vector dependencies.

---

### Phase 5 — Promotion, backup and operational hardening

```xml
<Phase-5 id="PH-005" name="PromotionBackupAndOps">
  <step-1 module="M-019">DraftPromotionService</step-1>
  <step-2 module="M-017">BackupService manual Git backup</step-2>
  <step-3 module="M-001">health_check and admin tools</step-3>
</Phase-5>
```

**Rationale:** promotion/move и backup являются operational risk surfaces and should be verified before semantic/wiki expansion.

---

### Phase 6 — Wiki propose-only automation

```xml
<Phase-6 id="PH-006" name="WikiProposeOnlyAutomation">
  <step-1 module="M-018">WikiUpdateService propose-only diff generation</step-1>
  <step-2 module="M-014">refresh_wiki_folder as index refresh, not autonomous wiki write</step-2>
</Phase-6>
```

**Rationale:** wiki automation наиболее рискованна по содержанию, поэтому идёт после core safety and retrieval.

---

### Phase 7 — Optional semantic retrieval

```xml
<Phase-7 id="PH-007" name="OptionalSemanticRetrieval">
  <step-1 module="M-016">VectorSearchAdapter sqlite_vec first</step-1>
  <step-2 module="M-010">hybrid search and similar notes</step-2>
</Phase-7>
```

**Rationale:** semantic retrieval полезен, но не должен быть MVP dependency. `sqlite-vec` предпочтительнее Qdrant для local-first deployment.

---

## 15. MVP boundaries

### В MVP входит

```text
Streamable HTTP over Tailscale/WireGuard
Obsidian Sync-aware revision checks
sha256(content) revision
Auth profiles
PolicyGuard
safe path resolution
symlink deny
create_note
append_note
edit_note with constrained patch format
write_note for trusted_writer
single move/promote contract
machine-readable audit
LOG.md projection
SQLite FTS5
WAL + single writer queue
affected_paths index refresh
health_check
manual Git backup
```

### В MVP не входит или включается минимально

```text
Qdrant
autonomous wiki write
rollback API
complex semantic search
bulk operations
delete
free-form regex patch
unrestricted move/rename
```

### После MVP

```text
sqlite-vec semantic search
drafts/ fully enabled
promote_note workflow expansion
get_note_history
restore_note_revision
advanced WikiUpdateService
hybrid ranking
```

**Rationale:** MVP должен закрыть безопасность, контроль записи, поиск и аудит без vector/rollback/bulk complexity.

---

## 16. Risk assessment

| Risk ID | Risk | Mitigation | Stop condition |
|---|---|---|---|
| `R-001` | Агент перезапишет внешнее изменение из Obsidian Sync | `sha256(content)` expected_revision + recheck before write | `SC-003` |
| `R-002` | Mutation/index circular dependency | `affected_paths` result; M-001/background worker calls M-014 | `SC-002` |
| `R-003` | Denylisted content leaked by retrieval | policy at indexing and response time | `SC-004` |
| `R-004` | Symlink escapes Vault sandbox | deny path if any component is symlink | `SC-005` |
| `R-005` | `write_note` destroys file | trusted profile, dry-run, diff, revision recheck, audit | `SC-001` / `SC-003` |
| `R-006` | `70_Wiki` corrupted by autonomous synthesis | propose-only diff generation | `SC-006` |
| `R-007` | SQLite locked under concurrent index writes | WAL + busy_timeout + single writer queue | phase gate for `V-M-013` |
| `R-008` | Audit trail lost or corrupted | authoritative SQLite/NDJSON audit + LOG.md projection | `V-M-009` failure gate |

---

## 17. Approval checkpoint

```xml
<architecture-approval-draft id="AAD-002">
  <decision id="ADR-001">Streamable HTTP через Tailscale/WireGuard</decision>
  <decision id="ADR-002">Vault синхронизируется через Obsidian Sync</decision>
  <decision id="ADR-003">Vault Markdown files are source of truth</decision>
  <decision id="ADR-004">revision = sha256(content)</decision>
  <decision id="ADR-005">mtime and size are indexing metadata only</decision>
  <decision id="ADR-006">All writes pass through PolicyGuard, ChangePlanner, LockAndRevisionService</decision>
  <decision id="ADR-007">edit_note is allowed only with constrained patch format, dry-run and expected_revision</decision>
  <decision id="ADR-008">write_note is allowed for trusted_writer with dry-run and expected_revision</decision>
  <decision id="ADR-009">Single move/promote allowed for trusted profile with dry-run; no bulk move</decision>
  <decision id="ADR-010">No delete operations</decision>
  <decision id="ADR-011">Machine-readable audit is authoritative; LOG.md is human-readable projection</decision>
  <decision id="ADR-012">Mutation result returns affected_paths; M-008 must not call M-014 directly</decision>
  <decision id="ADR-013">SQLite FTS5 is required baseline index</decision>
  <decision id="ADR-014">sqlite-vec is preferred future semantic backend</decision>
  <decision id="ADR-015">SQLite uses WAL and single writer queue</decision>
  <decision id="ADR-016">Hybrid process-level and filesystem-level locking</decision>
  <decision id="ADR-017">All symlinks are denied in MVP</decision>
  <decision id="ADR-018">drafts/promote workflow is supported by architecture and can be enabled after MVP</decision>
  <decision id="ADR-019">70_Wiki automation is propose-only diff generation</decision>
  <decision id="ADR-020">Rollback is manual Git backup in MVP</decision>
</architecture-approval-draft>
```

После approval этот draft можно превратить в GRACE artifacts:

```text
docs/requirements.xml
docs/technology.xml
docs/development-plan.xml
docs/verification-plan.xml
docs/knowledge-graph.xml
```
