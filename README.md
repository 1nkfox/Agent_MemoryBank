# Obsidian Memory MCP Server

MCP (Model Context Protocol) server for agent-mediated reading, mutation, retrieval, promotion, backup, and
wiki-proposal workflows over an Obsidian vault — with policy enforcement, audit logging, and structured index refresh.

Version: **0.1.0 MVP**

## Architecture

```
 M-001  MCPServerEntrypoint  (src/memory_mcp/server.py)  ← this module
 ├─ M-002  AuthService        (auth.py)
 ├─ M-003  VaultPolicyGuard   (policy.py)
 ├─ M-004  ReadService        (read_service.py)
 ├─ M-005  VaultFilesystem    (vault_fs.py)
 ├─ M-006  ChangePlanner      (change_planner.py)
 ├─ M-007  ConcurrencyGuard   (concurrency.py)
 ├─ M-008  VaultMutationService (mutation_service.py)
 ├─ M-009  AuditLogger        (audit_log.py)
 ├─ M-010  RetrievalService   (retrieval_service.py)
 ├─ M-011  ConfigService      (config.py)
 ├─ M-012  ObservabilityService (observability.py)
 ├─ M-013  IndexRepository    (index_repo.py)
 ├─ M-014  IndexRefreshService (index_refresh.py)
 ├─ M-015  MarkdownParser     (markdown_parser.py)
 ├─ M-016  VectorAdapter      (vector_adapter.py)
 ├─ M-017  BackupService      (backup_service.py)
 ├─ M-018  WikiUpdateService  (wiki_update_service.py)
 └─ M-019  DraftPromotionService (draft_promotion_service.py)
 ├─ M-020  MCPTransport         (transport.py)
```
## Quick Start

### Prerequisites

- Python >= 3.11

### Install

```bash
pip install -e ".[dev]"
```

### Configure

Create a config JSON file. API keys can be embedded directly or loaded from an environment variable:

**Option A: Direct keys** (for development only):

```json
{
  "vault": { "root": "/path/to/your/obsidian/vault" },
  "server": { "host": "127.0.0.1", "port": 8080 },
  "auth": {
    "api_keys": {
      "your-api-key-here": "readonly_agent",
      "another-api-key": "trusted_writer"
    }
  },
```

**Option B: Env-based keys** (for production — secrets stay out of config):

```json
{
  "vault": { "root": "/path/to/your/obsidian/vault" },
  "server": { "host": "127.0.0.1", "port": 8080 },
  "auth": {
    "api_keys_from_env": "MEMORY_MCP_API_KEYS"
  },
```

Then set the env variable:

```bash
export MEMORY_MCP_API_KEYS='{"sk-xxx": "admin", "sk-yyy": "readonly_agent"}'
```

Complete config structure:
    "allowlist_roots": ["memory/", "inbox/", "summaries/", "70_Wiki/"],
    "denylist_roots": ["private/", "secrets/"],
    "propose_only_roots": ["70_Wiki/"],
    "allowed_operations": ["read", "create", "append", "edit", "write", "promote", "propose", "search", "refresh", "backup"]
  },
  "index": {
    "db_path": "/path/to/your/vault/.obsidian/index.db",
    "fts_enabled": true,
    "fts_language": "english"
  },
  "vector": { "backend": "disabled" },
  "audit": {
    "enabled": true,
    "audit_db_path": "/path/to/your/vault/.obsidian/audit.db",
    "log_md_path": "/path/to/your/vault/LOG.md"
  },
  "backup": {
    "enabled": true,
    "git_path": "/path/to/your/vault/.git"
  }
}
```

### Run

```bash
export MEMORY_MCP_CONFIG=/path/to/config.json
memory-mcp
```

Or check configuration without starting the server:

```bash
export MEMORY_MCP_CONFIG=/path/to/config.json
export MEMORY_MCP_TRANSPORT=check
memory-mcp
```

The server starts a Streamable HTTP MCP transport on the configured host:port.
MCP clients connect to `POST http://host:port/message` with Bearer token auth.

### Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
python -m pytest

# Lint
python -m ruff check .

# Type check
python -m mypy src tests
```

## MCP Tools

| Tool | Description | Auth Profile |
|---|---|---|
| `read_note` | Read a note by vault-relative path | readonly_agent+ |
| `search_notes` | Full-text / vector search across indexed notes | readonly_agent+ |
| `create_note` | Create a new note | trusted_writer, admin |
| `append_note` | Append content to an existing note | trusted_writer, admin, cron_summarizer |
| `edit_note` | Patch-edit a note (section-based) | trusted_writer, admin |
| `write_note` | Full overwrite of a note | trusted_writer, admin |
| `promote_note` | Move a draft from inbox to memory | admin |
| `propose_wiki_update` | Generate a diff proposal for wiki (read-only) | any (propose) |
| `refresh_paths` | Re-index specified paths | admin |
| `health_check` | Server health + registered modules | any authenticated |
| `backup_vault` | Trigger a git-backed vault backup | admin |
| `backup_health` | Check backup repository health | admin |

## Auth & Policy

API keys are mapped to profiles via `auth.api_keys` in config. Each profile has a fixed permission matrix:

| Profile | Operations |
|---|---|
| `readonly_agent` | read, search, propose |
| `trusted_writer` | read, search, create, append, edit, write, propose |
| `wiki_maintainer` | read, search, propose |
| `cron_summarizer` | read, search, append, propose |
| `admin` | all operations |

## MVP Limitations

1. **No delete, bulk_move, or unrestricted rename** — These operations are permanently denied at the policy
   layer (MVP constraint).

2. **Vector backend = disabled** — The default config uses `"backend": "disabled"`. Semantic (vector-based)
   retrieval requires a working Qdrant or sqlite-vec integration.

3. **Wiki updates are propose-only** — `propose_wiki_update` returns a diff for human review. Direct
   writes to `70_Wiki/` are policy-denied for all profiles.

4. **No network-layer encryption** — API keys are passed as Bearer tokens (intended for use over private
   Tailscale/WireGuard networks only). No TLS termination built into the server.

## Verification Commands

```bash
python -m pytest                    # Run all tests
python -m ruff check .              # Lint
python -m mypy src tests            # Type check
```

## Known Blockers for Production Deploy

| # | Blocker | Status |
|---|---|---|
| 1 | Production-grade key management (Vault/KMS/Rotated) | Env-based injection added; key rotation TBD |
| 2 | Integration tests against a real Obsidian vault | Not started |
| 3 | Vector backend for semantic search | `disabled` by default; sqlite_vec / Qdrant TBD |
| 4 | Graceful shutdown / health endpoint for orchestration | `/health` endpoint implemented; liveness probes TBD | |
