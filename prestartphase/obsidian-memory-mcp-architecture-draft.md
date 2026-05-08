# Архитектурный черновик MCP сервера памяти для Obsidian Vault

Дата: 2026-04-29
Статус: prestart draft / approval checkpoint

> Документ является первичным архитектурным черновиком. По GRACE-процессу это не финальная архитектура: сначала согласуются модули, зависимости, границы ответственности, верификация и спорные решения. После approval архитектуру можно оформить в `docs/*.xml`.

---

## 1. Архитектурная цель

Проект: **личный MCP-сервер доступа к Obsidian Vault на VPS**, выступающий как единый банк памяти для агентов: Codex, Claude Code, OpenClaw и других MCP-клиентов.

Главные качества:

1. **Безопасность Vault**
   - запрет `delete`;
   - запрет bulk rename/move;
   - allowlist для записи;
   - denylist для чтения/записи;
   - `dry-run` перед изменениями;
   - `propose_only` режим.

2. **Конкурентная работа агентов**
   - защита от перезаписи;
   - file-level lock;
   - optimistic concurrency через revision/hash;
   - журналирование всех изменений в `LOG.md`.

3. **Быстрый retrieval**
   - keyword search;
   - full-text search;
   - semantic search опционально;
   - hybrid search опционально;
   - фильтры по folders/tags/frontmatter;
   - поиск похожих заметок;
   - поиск источников для wiki-страниц.

4. **Простота деплоя**
   - Python + FastMCP;
   - локальный Vault на VPS;
   - SQLite FTS как базовый индекс;
   - Qdrant как optional adapter, не обязательная зависимость.

5. **GRACE-ready**
   - архитектура через ID-based XML contracts;
   - модули как DAG;
   - verification refs;
   - trace/log anchors;
   - строгие границы public interfaces.

---

## 2. Ключевые архитектурные решения

### ADR-001: FastMCP + Python как базовый runtime

**Решение:** использовать Python + FastMCP как основной MCP слой.

**Rationale:**

- FastMCP быстро дает MCP-compatible tools.
- Python хорошо подходит для работы с файловой системой, Markdown, YAML frontmatter, SQLite FTS, embedding providers.
- Простота поддержки важнее абсолютной производительности, потому что это personal-use сервер.
- Python-экосистема достаточна для Obsidian Vault tooling.

**Trade-off:** Python медленнее Rust/Go, но bottleneck здесь скорее I/O, индекс и embeddings, а не CPU-bound runtime.

---

### ADR-002: SQLite FTS5 как обязательный search index, Qdrant как optional semantic backend

**Решение:** базовый индекс делать на SQLite:

- metadata table;
- full-text FTS5 table;
- file path/revision/hash;
- tags/frontmatter;
- optional embedding table или adapter к Qdrant.

Qdrant оставить как опциональный модуль.

**Rationale:**

- Для личного Vault SQLite FTS часто достаточно.
- Убираем сложность деплоя и обслуживания Qdrant на старте.
- Semantic search можно добавить позже через adapter.
- SQLite проще бекапить вместе с Vault.
- FTS5 обеспечивает быстрый full-text search без внешнего сервиса.

**Когда нужен Qdrant:**

- Vault очень большой;
- нужен качественный semantic retrieval;
- требуется hybrid ranking на embeddings;
- нужны быстрые похожие заметки по embedding distance.

**Рекомендуемый старт:** SQLite FTS5 first, Qdrant optional.

---

### ADR-003: Vault остается source of truth

**Решение:** Markdown-файлы Obsidian Vault — primary data source. Индекс является производным и может быть пересобран.

**Rationale:**

- Vault уже размечен по LLM Wiki принципу.
- Синхронизация через Obsidian/Vault остается внешней задачей.
- Минимизируем риск corruption: если индекс сломался, его можно пересоздать из Markdown.
- Git backup применим к реальным Markdown-файлам.

---

### ADR-004: Все write операции идут через Change Pipeline

**Решение:** любые `write/create/edit/append/index_update/wiki_update/log_write` проходят через единый pipeline:

```text
MCP Tool
  -> Auth
  -> Policy Guard
  -> Dry Run Planner
  -> Lock Manager
  -> Revision Check
  -> File Operation Executor
  -> LOG.md Append
  -> Index Update Event
```

**Rationale:**

- Нельзя позволить разным tools писать по-разному.
- Единый pipeline упрощает аудит, тестирование и безопасность.
- Dry-run/propose-only становится системным правилом, а не флагом отдельных функций.

---

### ADR-005: Защита от перезаписи через lock + expected_revision

**Решение:**

- для каждого изменяемого файла использовать file-level lock;
- read возвращает `revision` / `content_hash` / `mtime`;
- edit/write требует `expected_revision`;
- если revision не совпал — операция отклоняется с conflict response.

**Rationale:**

- Несколько агентов могут писать одновременно.
- Lock защищает от одновременной записи.
- Revision защищает от stale read → overwrite.
- Это проще и надежнее, чем пытаться делать автоматический merge.

---

### ADR-006: `propose_only` и `dry_run` как policy-level режимы

**Решение:**

- `dry_run=true` возвращает план изменения без записи;
- `propose_only=true` запрещает actual mutation, даже если tool вызван как write;
- для опасных областей Vault можно включать propose-only всегда.

**Rationale:**

- Агент может ошибиться.
- Dry-run позволяет пользователю/агенту проверить diff.
- Propose-only полезен для cronjob и low-trust агентов.

---

### ADR-007: Git backup как рекомендуемый backup backend

**Решение:** использовать Git для snapshot backup Vault и индекса/metadata по политике.

**Rationale:**

- Git хорошо подходит для Markdown Vault.
- Можно видеть diff изменений агентов.
- Можно откатиться.
- Можно делать cron-based commits.
- Не надо изобретать backup format.

**Ограничение:** индекс SQLite не обязательно коммитить. Лучше индекс пересоздаваемый. Коммитить можно только metadata snapshots или manifest.

---

## 3. Предлагаемые MCP tools

### Read tools

```xml
<tool-001 name="read_note">
  <purpose>Read a markdown file from allowed Vault paths.</purpose>
</tool-001>

<tool-002 name="check_file_exists">
  <purpose>Check whether a path exists and is accessible under read policy.</purpose>
</tool-002>

<tool-003 name="list_allowed_paths">
  <purpose>Expose allowed read/write roots and effective policy summary to trusted clients.</purpose>
</tool-003>
```

### Mutation tools

```xml
<tool-010 name="create_note">
  <purpose>Create a new Markdown note in an allowlisted directory.</purpose>
</tool-010>

<tool-011 name="write_note">
  <purpose>Replace full file content only when policy, dry-run and revision checks pass.</purpose>
</tool-011>

<tool-012 name="edit_note">
  <purpose>Apply a scoped patch to existing file content with expected_revision protection.</purpose>
</tool-012>

<tool-013 name="append_note">
  <purpose>Append content to an existing or newly created note under policy constraints.</purpose>
</tool-013>

<tool-014 name="write_log">
  <purpose>Append audit/event records to LOG.md through the same safe mutation pipeline.</purpose>
</tool-014>
```

### Index/retrieval tools

```xml
<tool-020 name="search_notes">
  <purpose>Run keyword, full-text, semantic or hybrid retrieval over indexed Vault notes.</purpose>
</tool-020>

<tool-021 name="find_similar_notes">
  <purpose>Find notes similar to a note or query, using FTS and optionally embeddings.</purpose>
</tool-021>

<tool-022 name="find_wiki_sources">
  <purpose>Find source notes relevant to a planned or existing wiki page.</purpose>
</tool-022>

<tool-023 name="refresh_index">
  <purpose>Rebuild or incrementally update the search index for selected Vault paths.</purpose>
</tool-023>

<tool-024 name="refresh_wiki_folder">
  <purpose>Update or propose updates for the root 70_Wiki folder.</purpose>
</tool-024>
```

### Admin/maintenance tools

```xml
<tool-030 name="backup_vault">
  <purpose>Create a Git-backed or filesystem-backed backup snapshot.</purpose>
</tool-030>

<tool-031 name="health_check">
  <purpose>Report server, Vault, index, policy, and lock health.</purpose>
</tool-031>
```

---

## 4. Module breakdown table

| ID | Name | Type | Purpose | Dependencies | Target paths | Verification ref |
|---|---|---|---|---|---|---|
| `M-001` | `MCPServerEntrypoint` | `ENTRY_POINT` | Exposes FastMCP tools and maps requests into application services. | `M-002`, `M-003`, `M-004`, `M-008`, `M-010` | `src/memory_mcp/server.py` | `V-M-001` |
| `M-002` | `AuthIdentityService` | `CORE_LOGIC` | Authenticates agent API keys and resolves identity, role, and policy profile. | `M-011` | `src/memory_mcp/auth.py` | `V-M-002` |
| `M-003` | `VaultPolicyGuard` | `CORE_LOGIC` | Enforces allowlist, denylist, no-delete, no-bulk-move, dry-run, propose-only rules. | `M-002`, `M-011`, `M-012` | `src/memory_mcp/policy.py` | `V-M-003` |
| `M-004` | `VaultReadService` | `CORE_LOGIC` | Provides safe read/list/exists operations over allowed Vault paths. | `M-003`, `M-005`, `M-012` | `src/memory_mcp/read_service.py` | `V-M-004` |
| `M-005` | `VaultFilesystemRepository` | `DATA_LAYER` | Performs normalized path resolution, file reads, atomic writes and metadata inspection. | `M-011`, `M-012` | `src/memory_mcp/vault_fs.py` | `V-M-005` |
| `M-006` | `ChangePlanner` | `CORE_LOGIC` | Builds dry-run diffs, validates expected revisions, and creates executable change plans. | `M-003`, `M-005`, `M-007`, `M-012` | `src/memory_mcp/change_planner.py` | `V-M-006` |
| `M-007` | `LockAndRevisionService` | `CORE_LOGIC` | Provides file-level locks and optimistic concurrency checks. | `M-005`, `M-011`, `M-012` | `src/memory_mcp/concurrency.py` | `V-M-007` |
| `M-008` | `VaultMutationService` | `CORE_LOGIC` | Executes create/write/edit/append/log operations through the safe change pipeline. | `M-003`, `M-005`, `M-006`, `M-007`, `M-009`, `M-012`, `M-014` | `src/memory_mcp/mutation_service.py` | `V-M-008` |
| `M-009` | `AuditLogService` | `CORE_LOGIC` | Appends structured records to `LOG.md` and emits trace events for all material actions. | `M-005`, `M-007`, `M-012` | `src/memory_mcp/audit_log.py` | `V-M-009` |
| `M-010` | `RetrievalService` | `CORE_LOGIC` | Provides keyword, FTS, semantic, hybrid, similar-notes and wiki-source retrieval. | `M-003`, `M-013`, `M-015`, `M-016`, `M-012` | `src/memory_mcp/retrieval_service.py` | `V-M-010` |
| `M-011` | `ConfigService` | `UTILITY` | Loads server, Vault, auth, policy, index and backup configuration. | none | `src/memory_mcp/config.py` | `V-M-011` |
| `M-012` | `ObservabilityService` | `UTILITY` | Provides structured logs, request IDs, trace anchors and error taxonomy. | `M-011` | `src/memory_mcp/observability.py` | `V-M-012` |
| `M-013` | `IndexRepository` | `DATA_LAYER` | Maintains SQLite metadata and FTS index derived from Vault files. | `M-005`, `M-011`, `M-012` | `src/memory_mcp/index_repo.py` | `V-M-013` |
| `M-014` | `IndexRefreshService` | `CORE_LOGIC` | Rebuilds or incrementally updates the index, including root `index` and `70_Wiki` workflows. | `M-003`, `M-005`, `M-013`, `M-015`, `M-012` | `src/memory_mcp/index_refresh.py` | `V-M-014` |
| `M-015` | `MarkdownSemanticParser` | `UTILITY` | Extracts frontmatter, tags, links, headings, wiki sections and LLM Wiki metadata. | `M-012` | `src/memory_mcp/markdown_parser.py` | `V-M-015` |
| `M-016` | `VectorSearchAdapter` | `INTEGRATION` | Optional adapter for semantic embeddings and Qdrant-backed vector search. | `M-011`, `M-012` | `src/memory_mcp/vector_adapter.py` | `V-M-016` |
| `M-017` | `BackupService` | `INTEGRATION` | Creates backup snapshots, preferably Git-backed, without mutating Vault content unexpectedly. | `M-005`, `M-011`, `M-012` | `src/memory_mcp/backup_service.py` | `V-M-017` |
| `M-018` | `WikiUpdateService` | `CORE_LOGIC` | Generates or proposes updates for `70_Wiki` pages from source notes and retrieval results. | `M-003`, `M-006`, `M-008`, `M-010`, `M-015`, `M-012` | `src/memory_mcp/wiki_update_service.py` | `V-M-018` |

---

## 5. Dependency DAG

### Text DAG

```text
M-011 ConfigService
  -> M-002 AuthIdentityService
  -> M-003 VaultPolicyGuard
  -> M-005 VaultFilesystemRepository
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

M-005 VaultFilesystemRepository
  -> M-004 VaultReadService
  -> M-006 ChangePlanner
  -> M-007 LockAndRevisionService
  -> M-008 VaultMutationService
  -> M-009 AuditLogService
  -> M-013 IndexRepository
  -> M-014 IndexRefreshService
  -> M-017 BackupService

M-007 LockAndRevisionService
  -> M-006 ChangePlanner
  -> M-008 VaultMutationService
  -> M-009 AuditLogService

M-015 MarkdownSemanticParser
  -> M-010 RetrievalService
  -> M-014 IndexRefreshService
  -> M-018 WikiUpdateService

M-013 IndexRepository
  -> M-010 RetrievalService
  -> M-014 IndexRefreshService

M-016 VectorSearchAdapter
  -> M-010 RetrievalService

M-006 ChangePlanner
  -> M-008 VaultMutationService
  -> M-018 WikiUpdateService

M-008 VaultMutationService
  -> M-001 MCPServerEntrypoint
  -> M-018 WikiUpdateService

M-010 RetrievalService
  -> M-001 MCPServerEntrypoint
  -> M-018 WikiUpdateService

M-014 IndexRefreshService
  -> M-001 MCPServerEntrypoint

M-017 BackupService
  -> M-001 MCPServerEntrypoint

M-018 WikiUpdateService
  -> M-001 MCPServerEntrypoint
```

### Circular dependency check

На уровне архитектурного DAG **циклов нет**, если соблюдаем правило:

> `M-008 VaultMutationService` не должен напрямую вызывать `M-014 IndexRefreshService`.

Вместо этого mutation pipeline должен публиковать событие/return flag:

```text
mutation_completed -> index_refresh_requested
```

А `MCPServerEntrypoint` или background task вызывает `M-014`.

**Rationale:** если `MutationService -> IndexRefreshService -> FilesystemRepository -> MutationService`, появится риск циклической связности и сложной отладки. Это ограничение зафиксировано в контрактах: `<POST_CONDITION>` в `M-008 VaultMutationService` и `<INVOCATION_CONTRACT>` в `M-014 IndexRefreshService`.

---

## 6. Public contracts в GRACE-стиле

### `M-003 VaultPolicyGuard`

```xml
<M-003 NAME="VaultPolicyGuard" TYPE="CORE_LOGIC">
  <PURPOSE>Enforces path, operation, dry-run, and propose-only policies before any Vault access is performed.</PURPOSE>
  <SCOPE>
    <INCLUDES>read path authorization</INCLUDES>
    <INCLUDES>write path authorization</INCLUDES>
    <INCLUDES>denylist enforcement</INCLUDES>
    <INCLUDES>delete prohibition</INCLUDES>
    <INCLUDES>bulk rename and move prohibition</INCLUDES>
    <INCLUDES>propose_only enforcement</INCLUDES>
    <INCLUDES>dry_run requirement validation</INCLUDES>
    <EXCLUDES>actual file mutation</EXCLUDES>
    <EXCLUDES>authentication secret storage</EXCLUDES>
  </SCOPE>
  <DEPENDS>
    <MODULE_REF>M-002</MODULE_REF>
    <MODULE_REF>M-011</MODULE_REF>
    <MODULE_REF>M-012</MODULE_REF>
  </DEPENDS>
  <PUBLIC_INTERFACE>
    <fn-authorize_read />
    <fn-authorize_write />
    <fn-authorize_search />
    <fn-authorize_index_refresh />
    <fn-resolve_effective_policy />
  </PUBLIC_INTERFACE>
  <RATIONALE>Centralized policy prevents individual tools from bypassing Vault safety rules.</RATIONALE>
</M-003>
```

### `M-005 VaultFilesystemRepository`

```xml
<M-005 NAME="VaultFilesystemRepository" TYPE="DATA_LAYER">
  <PURPOSE>Performs all direct Vault filesystem access: safe path resolution, file reads, atomic writes, metadata inspection, and symlink handling.</PURPOSE>
  <SCOPE>
    <INCLUDES>path normalization and safe resolution</INCLUDES>
    <INCLUDES>path traversal blocking (../, symlinks outside Vault root)</INCLUDES>
    <INCLUDES>symlink detection and safe handling</INCLUDES>
    <INCLUDES>reading file content with encoding detection (UTF-8, with BOM tolerance)</INCLUDES>
    <INCLUDES>atomic file writes via temp-file + rename</INCLUDES>
    <INCLUDES>metadata extraction (mtime, size, content_hash SHA-256)</INCLUDES>
    <INCLUDES>directory listing within allowed paths</INCLUDES>
    <INCLUDES>existence checks respecting policy</INCLUDES>
    <EXCLUDES>policy decisions (delegated to M-003 VaultPolicyGuard)</EXCLUDES>
    <EXCLUDES>locking (delegated to M-007 LockAndRevisionService)</EXCLUDES>
    <EXCLUDES>content parsing (delegated to M-015 MarkdownSemanticParser)</EXCLUDES>
  </SCOPE>
  <DEPENDS>
    <MODULE_REF>M-011</MODULE_REF>
    <MODULE_REF>M-012</MODULE_REF>
  </DEPENDS>
  <PUBLIC_INTERFACE>
    <fn-resolve_safe_path>
      <purpose>Normalize and validate a Vault-relative path, blocking any traversal outside the Vault root.</purpose>
      <returns>Absolute safe path or error if denied.</returns>
    </fn-resolve_safe_path>
    <fn-read_file>
      <purpose>Read file content with encoding detection.</purpose>
      <returns>File content as str, along with mtime, content_hash, and size metadata.</returns>
    </fn-read_file>
    <fn-atomic_write>
      <purpose>Write content to a file atomically (temp-file + rename). Never corrupts existing content on failure.</purpose>
      <returns>New content_hash after successful write.</returns>
    </fn-atomic_write>
    <fn-get_metadata>
      <purpose>Return mtime, size, content_hash, and existence status for a file path.</purpose>
    </fn-get_metadata>
    <fn-list_directory>
      <purpose>List files and subdirectories within an allowed directory path.</purpose>
    </fn-list_directory>
    <fn-check_exists>
      <purpose>Return whether a path exists and is accessible (file or directory).</purpose>
    </fn-check_exists>
  </PUBLIC_INTERFACE>
  <RATIONALE>VaultFilesystemRepository is the sole module allowed to touch the filesystem. All other modules route through it, enabling centralized path safety, symlink protection, and atomic write guarantees. It is the highest fan-in module (10 dependants) — its contract must be stabilized first.</RATIONALE>
</M-005>
```

### `M-006 ChangePlanner`

```xml
<M-006 NAME="ChangePlanner" TYPE="CORE_LOGIC">
  <PURPOSE>Converts requested mutations into safe, reviewable change plans before execution.</PURPOSE>
  <SCOPE>
    <INCLUDES>dry-run diff creation</INCLUDES>
    <INCLUDES>expected_revision validation plan</INCLUDES>
    <INCLUDES>operation normalization</INCLUDES>
    <INCLUDES>propose-only response generation</INCLUDES>
    <EXCLUDES>direct disk writes</EXCLUDES>
  </SCOPE>
  <DEPENDS>
    <MODULE_REF>M-003</MODULE_REF>
    <MODULE_REF>M-005</MODULE_REF>
    <MODULE_REF>M-007</MODULE_REF>
    <MODULE_REF>M-012</MODULE_REF>
  </DEPENDS>
  <PUBLIC_INTERFACE>
    <fn-plan_create />
    <fn-plan_write />
    <fn-plan_edit />
    <fn-plan_append />
    <fn-plan_log_append />
  </PUBLIC_INTERFACE>
  <RATIONALE>Planning mutations separately from execution makes dry-run and propose-only behavior verifiable.</RATIONALE>
</M-006>
```

### `M-007 LockAndRevisionService`

```xml
<M-007 NAME="LockAndRevisionService" TYPE="CORE_LOGIC">
  <PURPOSE>Provides file-level locks and optimistic concurrency checks to serialize concurrent agent writes and detect stale-read conflicts.</PURPOSE>
  <SCOPE>
    <INCLUDES>file-level lock acquisition with configurable timeout</INCLUDES>
    <INCLUDES>lock release (automatic on error or explicit)</INCLUDES>
    <INCLUDES>lock heartbeat for long-running operations</INCLUDES>
    <INCLUDES>revision computation (content_hash SHA-256 or equivalent)</INCLUDES>
    <INCLUDES>revision verification — reject if stored revision does not match expected_revision</INCLUDES>
    <INCLUDES>stale lock detection and forced release</INCLUDES>
    <INCLUDES>read-with-revision — return file content plus its current revision</INCLUDES>
    <EXCLUDES>policy authorization (delegated to M-003)</EXCLUDES>
    <EXCLUDES>actual file mutation (delegated to M-005 and M-008)</EXCLUDES>
  </SCOPE>
  <DEPENDS>
    <MODULE_REF>M-005</MODULE_REF>
    <MODULE_REF>M-011</MODULE_REF>
    <MODULE_REF>M-012</MODULE_REF>
  </DEPENDS>
  <PUBLIC_INTERFACE>
    <fn-acquire_lock>
      <purpose>Acquire an exclusive file-level lock for a given Vault path. Blocks up to timeout_ms, then fails with LOCK_TIMEOUT.</purpose>
      <returns>lock_handle (opaque token) or error.</returns>
    </fn-acquire_lock>
    <fn-release_lock>
      <purpose>Release a previously acquired lock by lock_handle.</purpose>
    </fn-release_lock>
    <fn-verify_revision>
      <purpose>Check that the stored revision for a file path matches expected_revision. Returns true/false.</purpose>
      <returns>bool — true if revision matches, false if stale (trigger STALE_REVISION rejection).</returns>
    </fn-verify_revision>
    <fn-compute_revision>
      <purpose>Compute the revision identifier for file content (SHA-256 content_hash + mtime). Used after successful writes.</purpose>
      <returns>revision string.</returns>
    </fn-compute_revision>
    <fn-read_with_revision>
      <purpose>Atomically read file content and return it together with the current revision, so the caller has a consistent snapshot for later revision checks.</purpose>
      <returns>content, revision, mtime.</returns>
    </fn-read_with_revision>
  </PUBLIC_INTERFACE>
  <RATIONALE>Without LockAndRevisionService, multiple MCP agents writing to the same Vault would silently overwrite each other's changes. Lock serializes concurrent writes; revision rejects stale-read → overwrite sequences. This is the critical component for multi-agent safety.</RATIONALE>
</M-007>
```

### `M-008 VaultMutationService`

```xml
<M-008 NAME="VaultMutationService" TYPE="CORE_LOGIC">
  <PURPOSE>Executes approved Vault mutations through locking, revision checks, atomic writes, audit logging, and index invalidation signals.</PURPOSE>
  <SCOPE>
    <INCLUDES>create_note</INCLUDES>
    <INCLUDES>write_note</INCLUDES>
    <INCLUDES>edit_note</INCLUDES>
    <INCLUDES>append_note</INCLUDES>
    <INCLUDES>write_log</INCLUDES>
    <INCLUDES>atomic file replace</INCLUDES>
    <INCLUDES>post-mutation audit event</INCLUDES>
    <INCLUDES>return index_refresh_needed signal flag</INCLUDES>
    <EXCLUDES>delete operations</EXCLUDES>
    <EXCLUDES>bulk rename or move</EXCLUDES>
    <EXCLUDES>semantic search ranking</EXCLUDES>
    <EXCLUDES>direct invocation of M-014 IndexRefreshService</EXCLUDES>
  </SCOPE>
  <DEPENDS>
    <MODULE_REF>M-003</MODULE_REF>
    <MODULE_REF>M-005</MODULE_REF>
    <MODULE_REF>M-006</MODULE_REF>
    <MODULE_REF>M-007</MODULE_REF>
    <MODULE_REF>M-009</MODULE_REF>
    <MODULE_REF>M-012</MODULE_REF>
  </DEPENDS>
  <PUBLIC_INTERFACE>
    <fn-create_note />
    <fn-write_note />
    <fn-edit_note />
    <fn-append_note />
    <fn-append_log />
  </PUBLIC_INTERFACE>
  <POST_CONDITION>
    <signal name="index_refresh_needed">
      <type>return flag (boolean) in mutation result</type>
      <semantics>Set to true when the executed mutation modifies file content in a way that requires M-014 IndexRefreshService to re-index the affected paths. The caller (M-001 MCPServerEntrypoint or background task) is responsible for invoking M-014. M-008 MUST NOT call M-014 directly.</semantics>
      <rationale>Prevents circular dependency: M-008 → M-014 → M-005 → … → M-008. The signal decouples mutation from index refresh.</rationale>
      <verification_ref>SC-001</verification_ref>
    </signal>
  </POST_CONDITION>
  <RATIONALE>A single mutation service ensures every write path receives identical safety guarantees. The index_refresh_needed signal is returned as a flag — never a direct call — to prevent circular coupling with M-014.</RATIONALE>
</M-008>
```

### `M-010 RetrievalService`

```xml
<M-010 NAME="RetrievalService" TYPE="CORE_LOGIC">
  <PURPOSE>Runs authorized retrieval queries over Vault-derived indexes and optional vector search backends.</PURPOSE>
  <SCOPE>
    <INCLUDES>keyword search</INCLUDES>
    <INCLUDES>SQLite FTS search</INCLUDES>
    <INCLUDES>semantic search when vector backend is enabled</INCLUDES>
    <INCLUDES>hybrid search</INCLUDES>
    <INCLUDES>folder filters</INCLUDES>
    <INCLUDES>tag and frontmatter filters</INCLUDES>
    <INCLUDES>similar note retrieval</INCLUDES>
    <INCLUDES>wiki source retrieval</INCLUDES>
    <EXCLUDES>direct mutation of notes</EXCLUDES>
  </SCOPE>
  <DEPENDS>
    <MODULE_REF>M-003</MODULE_REF>
    <MODULE_REF>M-013</MODULE_REF>
    <MODULE_REF>M-015</MODULE_REF>
    <MODULE_REF>M-016 optional="true" />
    <MODULE_REF>M-012</MODULE_REF>
  </DEPENDS>
  <PUBLIC_INTERFACE>
    <fn-search_notes />
    <fn-find_similar_notes />
    <fn-find_wiki_sources />
    <fn-explain_ranking />
  </PUBLIC_INTERFACE>
  <RATIONALE>Retrieval is separated from storage so the search backend can evolve from SQLite-only to hybrid/vector without changing MCP tools.</RATIONALE>
</M-010>
```

### `M-014 IndexRefreshService`

```xml
<M-014 NAME="IndexRefreshService" TYPE="CORE_LOGIC">
  <PURPOSE>Rebuilds or incrementally updates the search index (metadata + FTS + optional vectors) from Vault content. Invoked by M-001 or background task in response to the index_refresh_needed signal from M-008. NEVER called directly by M-008.</PURPOSE>
  <SCOPE>
    <INCLUDES>full index rebuild from Vault files</INCLUDES>
    <INCLUDES>incremental refresh for changed files</INCLUDES>
    <INCLUDES>scope filtering — refresh only allowed read paths</INCLUDES>
    <INCLUDES>stale index entry removal</INCLUDES>
    <INCLUDES>70_Wiki folder refresh workflow</INCLUDES>
    <INCLUDES>post-refresh summary (files added/updated/removed, errors)</INCLUDES>
    <EXCLUDES>direct invocation from M-008 VaultMutationService</EXCLUDES>
    <EXCLUDES>policy decisions (delegated to M-003)</EXCLUDES>
  </SCOPE>
  <DEPENDS>
    <MODULE_REF>M-003</MODULE_REF>
    <MODULE_REF>M-005</MODULE_REF>
    <MODULE_REF>M-013</MODULE_REF>
    <MODULE_REF>M-015</MODULE_REF>
    <MODULE_REF>M-012</MODULE_REF>
  </DEPENDS>
  <PUBLIC_INTERFACE>
    <fn-full_rebuild>
      <purpose>Scan all allowed Vault paths and rebuild the entire index from scratch.</purpose>
    </fn-full_rebuild>
    <fn-incremental_refresh>
      <purpose>Refresh only files changed since the last refresh (based on mtime/content_hash diff).</purpose>
    </fn-incremental_refresh>
    <fn-refresh_paths>
      <purpose>Refresh index entries for a specific set of Vault paths (e.g. paths returned by M-008's index_refresh_needed signal).</purpose>
      <param name="paths">List of Vault-relative paths that need re-indexing.</param>
    </fn-refresh_paths>
    <fn-refresh_wiki_folder>
      <purpose>Trigger a targeted refresh for the 70_Wiki folder only.</purpose>
    </fn-refresh_wiki_folder>
    <fn-remove_stale_entries>
      <purpose>Remove index entries for files that no longer exist in the Vault or are now denylisted.</purpose>
    </fn-remove_stale_entries>
  </PUBLIC_INTERFACE>
  <INVOCATION_CONTRACT>
    <rule id="IC-M014-001" severity="critical">
      <statement>M-008 VaultMutationService MUST NOT call any M-014 function directly. The only allowed coupling is: M-008 returns index_refresh_needed=true in its mutation result; the caller (M-001 MCPServerEntrypoint or background task) reads that flag and invokes M-014.refresh_paths() with the affected paths.</statement>
      <violation_consequence>Creates circular dependency M-008 → M-014 → M-005 → … → M-008, causing potential stack overflow and undebuggable side effects.</violation_consequence>
      <verification_ref>SC-001</verification_ref>
    </rule>
  </INVOCATION_CONTRACT>
  <RATIONALE>IndexRefreshService is the consumer of the index_refresh_needed signal from M-008. By separating index refresh from mutation execution, we keep the mutation pipeline linear and the index rebuildable. The invocation contract enforces this separation structurally, not by convention.</RATIONALE>
</M-014>
```

---

## 7. Data flow diagrams

### `DF-001` Save memory / append note

```text
Agent MCP Client
  -> M-001 MCPServerEntrypoint
  -> M-002 AuthIdentityService
  -> M-003 VaultPolicyGuard
  -> M-006 ChangePlanner
       - validate target folder
       - compute dry-run diff
       - check propose_only
       - prepare expected_revision check
  -> M-007 LockAndRevisionService
       - acquire file lock
       - verify expected_revision
  -> M-008 VaultMutationService
       - atomic append/write
  -> M-009 AuditLogService
       - append structured LOG.md entry
  -> return mutation result + new_revision + index_refresh_needed
  -> M-014 IndexRefreshService, sync or async
```

**Rationale:** write operation remains safe even if multiple agents attempt concurrent updates.

---

### `DF-002` Question answering / retrieval

```text
Agent MCP Client
  -> M-001 MCPServerEntrypoint
  -> M-002 AuthIdentityService
  -> M-003 VaultPolicyGuard
  -> M-010 RetrievalService
       - normalize query
       - apply folder/tag/frontmatter filters
       - query M-013 SQLite FTS
       - optionally query M-016 VectorSearchAdapter
       - merge/rerank results
  -> return notes/snippets/sources/relevance metadata
```

**Rationale:** retrieval should never touch mutation pipeline, reducing risk.

---

### `DF-003` Cronjob summary by OpenClaw

```text
OpenClaw Cron MCP Client
  -> M-001 MCPServerEntrypoint
  -> M-002 AuthIdentityService
       - cron identity
       - limited policy profile
  -> M-010 RetrievalService
       - query recent updates
       - query LOG.md/index metadata
  -> summary generated by agent
  -> M-008 VaultMutationService only if allowed
       - probably propose_only or append-only summary path
  -> M-009 AuditLogService
```

**Rationale:** cron identity should have more restrictive permissions than interactive trusted agents.

---

### `DF-004` Index refresh

```text
Manual/Cron/Mutation event
  -> M-001 MCPServerEntrypoint
  -> M-014 IndexRefreshService
  -> M-003 VaultPolicyGuard
       - authorize index refresh scope
  -> M-005 VaultFilesystemRepository
       - scan allowed folders
  -> M-015 MarkdownSemanticParser
       - extract frontmatter/tags/links/headings
  -> M-013 IndexRepository
       - update SQLite metadata + FTS
  -> M-016 VectorSearchAdapter optional
       - update embeddings/vector points
```

**Rationale:** index refresh is derived from Vault state and can be rebuilt.

---

### `DF-005` Wiki source discovery

```text
Agent wants wiki page sources
  -> M-001 MCPServerEntrypoint
  -> M-010 RetrievalService
       - search by title/topic/tags
       - find backlinks/forward links
       - similar notes
       - source candidates
  -> M-018 WikiUpdateService optional
       - create proposed wiki update plan
  -> M-006 ChangePlanner
       - dry-run/propose diff for 70_Wiki page
```

**Rationale:** wiki updates are high-risk content synthesis, so default should be propose-first.

---

## 8. Verification surface overview

### Critical flows

| Flow ID | Name | Must verify |
|---|---|---|
| `DF-001` | Save memory | policy, lock, revision, atomic write, audit log, index invalidation |
| `DF-002` | Retrieval | auth filtering, folder/tag filters, ranking stability, no denied paths leaked |
| `DF-003` | Cron summary | restricted identity, read-only/propose-only behavior, audit trail |
| `DF-004` | Index refresh | rebuild correctness, deleted/stale index entries, parser robustness |
| `DF-005` | Wiki source discovery | source traceability, propose-only diff, no unsupported mutation |

---

### Module-local checks

| Verification ref | Checks |
|---|---|
| `V-M-002` | valid API key accepted; invalid key rejected; identity profile loaded |
| `V-M-003` | allowlist permits; denylist overrides; delete blocked; bulk move blocked; propose_only blocks writes |
| `V-M-005` | path traversal blocked; symlinks handled safely; atomic write behavior |
| `V-M-006` | dry-run produces correct diff; expected_revision required for overwrite-like edits |
| `V-M-007` | concurrent writes serialize; stale revision rejected |
| `V-M-008` | create/write/edit/append use same pipeline; no direct bypass |
| `V-M-009` | LOG.md append is structured and cannot recursively corrupt logging |
| `V-M-010` | search filters do not leak denied folders; hybrid results explainable |
| `V-M-013` | FTS index matches Vault content after rebuild |
| `V-M-014` | incremental refresh equals full rebuild for changed files |
| `V-M-016` | vector backend disabled gracefully; Qdrant errors degrade to FTS |
| `V-M-017` | backup does not mutate Vault; git snapshot excludes secrets if configured |
| `V-M-018` | wiki update defaults to propose/dry-run unless explicitly allowed |

---

### Required trace/log anchors

```xml
<trace-anchor id="TA-001" name="auth.identity.resolved" />
<trace-anchor id="TA-002" name="policy.decision" />
<trace-anchor id="TA-003" name="change.plan.created" />
<trace-anchor id="TA-004" name="lock.acquired" />
<trace-anchor id="TA-005" name="revision.checked" />
<trace-anchor id="TA-006" name="vault.atomic_write.completed" />
<trace-anchor id="TA-007" name="audit_log.appended" />
<trace-anchor id="TA-008" name="index.refresh.started" />
<trace-anchor id="TA-009" name="index.refresh.completed" />
<trace-anchor id="TA-010" name="retrieval.query.completed" />
<trace-anchor id="TA-011" name="retrieval.denied_path.filtered" />
<trace-anchor id="TA-012" name="backup.snapshot.completed" />
```

---

### Stop conditions / replan triggers

```xml
<stop-condition id="SC-001">
  <condition>Any write path bypasses M-003 VaultPolicyGuard or M-006 ChangePlanner.</condition>
  <action>Stop implementation and replan mutation boundary.</action>
</stop-condition>

<stop-condition id="SC-002">
  <condition>Any retrieval response can expose denylisted path content or metadata.</condition>
  <action>Stop and redesign retrieval filtering.</action>
</stop-condition>

<stop-condition id="SC-003">
  <condition>Concurrent write test can lose an agent's update silently.</condition>
  <action>Stop and redesign lock/revision behavior.</action>
</stop-condition>

<stop-condition id="SC-004">
  <condition>Index is treated as source of truth instead of rebuildable derivative.</condition>
  <action>Stop and reassert Vault-first architecture.</action>
</stop-condition>

<stop-condition id="SC-005">
  <condition>Wiki update can mutate 70_Wiki without dry-run/propose policy approval.</condition>
  <action>Stop and tighten WikiUpdateService contract.</action>
</stop-condition>
```

---

## 9. Implementation order

### Phase 1 — Foundation and safety boundary

```xml
<Phase-1 name="FoundationAndPolicy">
  <step-1 module="M-011">ConfigService</step-1>
  <step-2 module="M-012">ObservabilityService</step-2>
  <step-3 module="M-002">AuthIdentityService</step-3>
  <step-4 module="M-003">VaultPolicyGuard</step-4>
  <step-5 module="M-005">VaultFilesystemRepository</step-5>
</Phase-1>
```

**Rationale:** нельзя строить tools до auth/policy/path safety.

---

### Phase 2 — Read-only MCP server

```xml
<Phase-2 name="ReadOnlyMCP">
  <step-1 module="M-001">MCPServerEntrypoint read tools</step-1>
  <step-2 module="M-004">VaultReadService</step-2>
</Phase-2>
```

**Rationale:** сначала безопасное чтение и проверка path policy.

---

### Phase 3 — Safe mutation pipeline

```xml
<Phase-3 name="SafeMutationPipeline">
  <step-1 module="M-007">LockAndRevisionService</step-1>
  <step-2 module="M-006">ChangePlanner</step-2>
  <step-3 module="M-009">AuditLogService</step-3>
  <step-4 module="M-008">VaultMutationService</step-4>
</Phase-3>
```

**Rationale:** write tools появляются только после dry-run, locking, revision и audit.

---

### Phase 4 — Search/index without vector DB

```xml
<Phase-4 name="SQLiteFTSRetrieval">
  <step-1 module="M-015">MarkdownSemanticParser</step-1>
  <step-2 module="M-013">IndexRepository SQLite FTS</step-2>
  <step-3 module="M-014">IndexRefreshService</step-3>
  <step-4 module="M-010">RetrievalService keyword/FTS</step-4>
</Phase-4>
```

**Rationale:** получаем быстрый reliable retrieval без внешних сервисов.

---

### Phase 5 — Backup and operational hardening

```xml
<Phase-5 name="BackupAndOps">
  <step-1 module="M-017">BackupService</step-1>
  <step-2 module="M-001">health_check and admin tools</step-2>
</Phase-5>
```

**Rationale:** перед semantic/vector усложнением стоит закрыть эксплуатационные риски.

---

### Phase 6 — Optional semantic/hybrid retrieval

```xml
<Phase-6 name="OptionalSemanticRetrieval">
  <step-1 module="M-016">VectorSearchAdapter</step-1>
  <step-2 module="M-010">hybrid search and similar notes</step-2>
</Phase-6>
```

**Rationale:** Qdrant подключается только после стабильного FTS baseline.

---

### Phase 7 — Wiki automation

```xml
<Phase-7 name="WikiAutomation">
  <step-1 module="M-018">WikiUpdateService propose-only first</step-1>
  <step-2 module="M-014">70_Wiki refresh workflow</step-2>
</Phase-7>
```

**Rationale:** генерация wiki-страниц наиболее рискованна, поэтому идет последней и с propose-first политикой.

---

## 10. Рекомендованная структура конфигурации

```xml
<config-shape id="CFG-001">
  <server>
    <host>127.0.0.1 or unix socket behind reverse proxy</host>
    <transport>stdio/http depending on MCP deployment mode</transport>
  </server>

  <vault>
    <root>/srv/obsidian/vault</root>
    <wiki_folder>70_Wiki</wiki_folder>
    <index_folder>index</index_folder>
    <log_file>LOG.md</log_file>
  </vault>

  <auth>
    <api_keys_file>/etc/memory-mcp/agents.json</api_keys_file>
    <default_profile>readonly</default_profile>
  </auth>

  <policy>
    <read_allowlist>
      <path>.</path>
    </read_allowlist>
    <write_allowlist>
      <path>inbox</path>
      <path>memory</path>
      <path>70_Wiki</path>
      <path>LOG.md</path>
    </write_allowlist>
    <denylist>
      <path>.obsidian</path>
      <path>.git</path>
      <path>private</path>
      <path>secrets</path>
    </denylist>
    <delete_allowed>false</delete_allowed>
    <bulk_move_allowed>false</bulk_move_allowed>
    <require_dry_run_for_write>true</require_dry_run_for_write>
  </policy>

  <index>
    <backend>sqlite_fts5</backend>
    <sqlite_path>/srv/obsidian/.memory-mcp/index.sqlite3</sqlite_path>
    <vector_backend>disabled or qdrant</vector_backend>
  </index>

  <backup>
    <backend>git</backend>
    <schedule>cron</schedule>
    <include_index>false</include_index>
  </backup>
</config-shape>
```

---

## 11. Security model

### Agent profiles

```xml
<agent-profile id="AP-001" name="readonly_agent">
  <can_read>true</can_read>
  <can_search>true</can_search>
  <can_write>false</can_write>
  <propose_only>true</propose_only>
</agent-profile>

<agent-profile id="AP-002" name="trusted_writer">
  <can_read>true</can_read>
  <can_search>true</can_search>
  <can_write>true</can_write>
  <requires_dry_run>true</requires_dry_run>
  <allowed_write_roots>inbox,memory,LOG.md</allowed_write_roots>
</agent-profile>

<agent-profile id="AP-003" name="wiki_maintainer">
  <can_read>true</can_read>
  <can_search>true</can_search>
  <can_write>true</can_write>
  <requires_dry_run>true</requires_dry_run>
  <default_propose_only_for>70_Wiki</default_propose_only_for>
</agent-profile>

<agent-profile id="AP-004" name="cron_summarizer">
  <can_read>true</can_read>
  <can_search>true</can_search>
  <can_write>append_only</can_write>
  <propose_only>false</propose_only>
  <allowed_write_roots>summaries,LOG.md</allowed_write_roots>
</agent-profile>
```

### Rationale

- Codex/Claude/OpenClaw могут иметь разные ключи.
- Ключ — не просто auth, а policy selector.
- Если один агент скомпрометирован, он не получает full Vault mutation rights.
- Cronjob не должен иметь такие же права, как интерактивный trusted writer.

---

## 12. Search/retrieval design

### Baseline without vector DB

```text
SQLite:
  notes(id, path, title, mtime, content_hash, frontmatter_json, tags_json)
  notes_fts(note_id, title, headings, body)
  links(source_note_id, target_path_or_title, link_type)
  sections(note_id, heading_path, content, content_hash)
```

Supported:

- keyword search;
- full-text search;
- folder filter;
- tag filter;
- frontmatter filter;
- source discovery by backlinks/forward links;
- similar-ish notes using FTS overlap, tags, links, headings.

**Rationale:** этого достаточно для первого reliable retrieval.

---

### Optional semantic layer

```text
Qdrant collections:
  vault_note_chunks
    point_id
    path
    chunk_id
    heading_path
    content_hash
    embedding_model
    metadata
```

Supported:

- semantic search;
- similar notes;
- hybrid search;
- better wiki source retrieval.

**Rationale:** semantic search полезен, но не должен быть required dependency.

---

### Hybrid ranking proposal

```xml
<ranking id="RANK-001">
  <factor name="fts_score" weight="0.45" />
  <factor name="semantic_score" weight="0.35" optional="true" />
  <factor name="tag_match" weight="0.10" />
  <factor name="folder_priority" weight="0.05" />
  <factor name="recency" weight="0.05" />
</ranking>
```

**Rationale:** explainable ranking лучше для агентов, чем opaque vector-only retrieval.

---

## 13. LOG.md format proposal

```markdown
## 2026-04-29T18:21:45+03:00 — memory-mcp event

- event_id: EVT-20260429-182145-001
- agent_id: claude-code
- operation: append_note
- path: memory/research/example.md
- dry_run: false
- propose_only: false
- old_revision: sha256:...
- new_revision: sha256:...
- policy_profile: trusted_writer
- result: success
- trace_id: req-...
```

**Rationale:** Markdown-friendly, Obsidian-readable, machine-parseable enough.

---

## 14. Risk assessment

### Risk R-001: агент перезапишет чужую запись

**Mitigation:**

- expected_revision required;
- file-level lock;
- stale revision rejection;
- append preferred over full write.

**Stop condition:** любой overwrite без revision check.

---

### Risk R-002: retrieval отдаст содержимое denylisted папки

**Mitigation:**

- denylist applied both at indexing and query response time;
- index refresh removes denied paths;
- retrieval response re-checks policy before returning snippets.

**Stop condition:** any denied path appears in result.

---

### Risk R-003: index рассинхронизируется с Vault

**Mitigation:**

- index is derivative;
- full rebuild command;
- content_hash tracking;
- incremental refresh test against full rebuild.

**Stop condition:** system requires index as sole source of truth.

---

### Risk R-004: LOG.md logging causes recursive mutation issue

**Mitigation:**

- audit log append has dedicated operation type;
- avoid logging audit-log-write as infinite nested event;
- log failures to structured server logs if LOG.md unavailable.

**Stop condition:** audit write recursively triggers unbounded audit writes.

---

### Risk R-005: Qdrant усложнит деплой

**Mitigation:**

- Qdrant optional;
- disabled by default;
- FTS baseline required;
- vector failure degrades to FTS.

**Stop condition:** app cannot start without vector DB.

---

### Risk R-006: wiki automation начнет портить `70_Wiki`

**Mitigation:**

- default propose-only for `70_Wiki`;
- dry-run diff required;
- source citations required;
- write only after explicit allow.

**Stop condition:** autonomous wiki update without review path.

---

## 15. Рекомендация по MVP

Для первого этапа я бы **не ставил Qdrant**.

MVP:

```xml
<mvp id="MVP-001">
  <include>M-001 MCPServerEntrypoint</include>
  <include>M-002 AuthIdentityService</include>
  <include>M-003 VaultPolicyGuard</include>
  <include>M-004 VaultReadService</include>
  <include>M-005 VaultFilesystemRepository</include>
  <include>M-006 ChangePlanner</include>
  <include>M-007 LockAndRevisionService</include>
  <include>M-008 VaultMutationService</include>
  <include>M-009 AuditLogService</include>
  <include>M-010 RetrievalService keyword+FTS only</include>
  <include>M-011 ConfigService</include>
  <include>M-012 ObservabilityService</include>
  <include>M-013 IndexRepository SQLite FTS5</include>
  <include>M-014 IndexRefreshService</include>
  <include>M-015 MarkdownSemanticParser</include>
  <exclude>M-016 VectorSearchAdapter until needed</exclude>
  <include>M-017 BackupService basic git snapshot</include>
  <defer>M-018 WikiUpdateService advanced generation</defer>
</mvp>
```

**Rationale:** такой MVP уже покрывает read/write/search/index/log/backup/security/concurrency без внешней векторной инфраструктуры.

---

## 16. Вопросы, которые нужно решить до финализации

Есть несколько архитектурных неопределенностей. Без них финализировать `docs/*.xml` преждевременно.

### Q-001: MCP transport

Как сервер будет доступен агентам?

Варианты:

1. **stdio локально через SSH/remote command**
   - проще security;
   - сложнее для нескольких внешних агентов.

2. **HTTP/SSE MCP endpoint за reverse proxy**
   - удобнее для нескольких агентов;
   - требует TLS, auth, rate limit.

3. **HTTP только через VPN/Tailscale/WireGuard**
   - хороший вариант для личного VPS;
   - меньше attack surface.

**Предварительная рекомендация:** HTTP MCP за Tailscale/WireGuard или reverse proxy with TLS + API key.

---

### Q-002: Нужно ли разрешать full `write_note`?

Для безопасности можно сделать:

- `create_note`;
- `append_note`;
- `edit_note` scoped patch;
- `write_note` только trusted admin.

**Предварительная рекомендация:** `write_note` disabled by default для агентов, кроме admin profile.

---

### Q-003: Где агенты должны писать память?

Нужна политика директорий:

```text
inbox/
memory/
research/
summaries/
70_Wiki/
LOG.md
```

Нужно определить write allowlist.

**Предварительная рекомендация:**

- обычные агенты: `inbox/`, `memory/`, `LOG.md`;
- cron: `summaries/`, `LOG.md`;
- wiki maintainer: `70_Wiki/` только propose-first.

---

### Q-004: Нужно ли индексировать весь Vault или только allowlisted read folders?

**Предварительная рекомендация:** индексировать только read-allowed folders и никогда не индексировать denylisted folders.

---

### Q-005: Embeddings provider

Если добавляем semantic search позже, нужно выбрать:

- локальная модель на VPS;
- external API;
- Ollama;
- OpenAI-compatible endpoint;
- fastembed.

**Предварительная рекомендация:** для старта не выбирать. Оставить `M-016` adapter contract.

---

## 17. Approval checkpoint

Предлагается согласовать такую архитектурную позицию:

```xml
<architecture-approval-draft id="AAD-001">
  <decision id="ADR-001">Python + FastMCP as primary runtime</decision>
  <decision id="ADR-002">SQLite FTS5 as required index backend</decision>
  <decision id="ADR-003">Qdrant optional, disabled in MVP</decision>
  <decision id="ADR-004">Vault Markdown files are source of truth</decision>
  <decision id="ADR-005">All writes pass through ChangePlanner + PolicyGuard + LockAndRevisionService</decision>
  <decision id="ADR-006">No delete, no bulk rename or move</decision>
  <decision id="ADR-007">Dry-run required for write-class operations</decision>
  <decision id="ADR-008">Propose-only available per agent profile and per folder</decision>
  <decision id="ADR-009">Git backup recommended for Vault snapshots</decision>
  <decision id="ADR-010">70_Wiki automation is propose-first</decision>
</architecture-approval-draft>
```

После approval можно оформить это как GRACE artifacts:

```text
docs/requirements.xml
docs/technology.xml
docs/development-plan.xml
docs/verification-plan.xml
docs/knowledge-graph.xml
```
