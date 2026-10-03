# PersonaGraph

**English** | [한국어](README_ko.md)

Local, CLI-first alignment memory for Claude Code and Codex.

Keep a small Markdown graph of user-confirmed choices and their reasons. Search public session history through aichat-search, verify exact message evidence, and preserve cited versions with encrypted local backups. Automatic capture produces review checkpoints; it never writes active alignment policy by itself.

**Early alpha.** The development environment is macOS. macOS, Linux and Windows CI are configured; their results are visible in Actions. Windows runs natively (see [Windows](#windows)); live Claude/Codex hook execution on Windows has not been verified. Full large-history performance, automatic Codex 80% usage observation, and long-term alignment-quality claims are outside the current release.

## Flow

[Explore the interactive workflows](https://fmsongx2.github.io/personagraph/) · [Diagram sources and checks](docs/diagrams/README.md)

The Archify diagrams explain startup routing and capture/search/review separately. Diagram labels are Korean; viewer controls use the built-in English UI. Download the standalone HTML to view offline.

```text
Native Claude / Codex transcripts
  -> lifecycle metadata queue -> incremental catalog + session search
  -> am search (sessions) -> am find (messages) -> am get/context
  -> reviewed evidence pin -> immutable local evidence + restic backup
  -> agent reviews original evidence -> local Markdown graph
```

Persona, project progress memory, and domain knowledge have different roles. This repository ships the persona/alignment notebook and evidence adapter. Existing project-memory and domain-knowledge tools can be linked from the notebook; they are not silently installed or treated as user preferences.

## Quick start

Prerequisites: Python 3.11+, [uv](https://docs.astral.sh/uv/), Cargo/Rust, Git, and [restic](https://restic.net/). No API key is required for the memory CLI itself. Native Claude/Codex clients still use their own authentication.

### Windows

Use `py -3` (or `python`) for `python3`, and `am.cmd` (or `./am` from Git Bash) for `./am`. Additionally:

- Rust needs the MSVC linker: Visual Studio Build Tools with the "Desktop development with C++" workload, then `rustup`.
- restic **0.17 or newer** (`scoop install restic` or the release zip). Windows restores only the evidence subtree (`snapshot:path`), because restoring the full path would also restore `C:\Users` metadata and fail.
- The active search generation is a `runtime/alignment-memory/search-current.ref` pointer file replaced atomically, instead of a symlink (symlinks need Developer Mode or admin rights). The backup password file gets a protected owner-only ACL instead of mode 0600.
- Hook and status-line commands are written with forward-slash paths that run unchanged in Git Bash, cmd and PowerShell; paths with spaces use their 8.3 short names. A previous status line is run through Git Bash, like Claude Code does.
- No `PYTHONUTF8` is required: files, pipes and hook I/O are explicitly UTF-8.

```sh
git clone https://github.com/FMsongX2/personagraph.git
cd personagraph
python3 scripts/init-memory.py                 # private local memory/, synthetic defaults
python3 scripts/install-aichat-search.py        # pinned upstream build and Python indexing environment
./am --help
```

Choose `python3 scripts/init-memory.py --persona yui` instead for an optional Korean sibling-role engineering persona. Customize the private profile locally. The provided names, decisions, and persona are examples; no maintainer profile or real conversation is included.

If you want global persona routing, preview it before applying:

```sh
python3 scripts/sync-global.py
python3 scripts/sync-global.py --apply          # backs up and writes Claude/Codex global instructions
python3 scripts/sync-global.py --check
```

First application replaces existing global instruction files after a local backup. Later applications refuse an unexpected manual edit. This is optional and is separate from installing capture hooks.

## Evidence CLI

```sh
./am index /absolute/path/to/native-session.jsonl --adapter codex
./am index /absolute/path/to/native-session.jsonl --adapter claude
./am search 'keyword' --limit 5
./am find 'literal phrase' --source '<source-key>' --limit 8
./am get --source '<source-key>' --id '<message-id>' --hash '<sha256>'
./am context --source '<source-key>' --id '<message-id>' --before 2
./am pin --source '<source-key>' --id '<message-id>' --hash '<sha256>'
```

`search` returns session candidates and pending-update metadata. Candidates are not verified message evidence. `find` scans one session with a default 2,000-message budget and reports incomplete scans. `get` verifies ID, role, and content hash. Read commands do not create disk schemas or writable catalog locks. `pin` saves cited evidence and runs a local restic backup; backup failure is reported separately from evidence saved.

## Automatic capture (opt-in)

```sh
python3 scripts/install-alignment-capture.py                   # preview
python3 scripts/install-alignment-capture.py --apply            # lifecycle hooks
python3 scripts/install-alignment-capture.py --apply --statusline # also observe Claude context usage
```

Existing hooks are retained. Review the new Codex definitions in `/hooks`; the installer does not write trust hashes or bypass review. Start or resume a client to pick up settings. Custom harness homes can be supplied with `--claude-home` and `--codex-home`.

The Claude status observer preserves an existing command status line. If none exists, it remains silent. An existing tool that rewrites statusLine may require manual integration; no Orca-specific script is assumed.

```sh
python3 scripts/alignment-capture.py status
python3 scripts/alignment-capture.py reviews
python3 scripts/alignment-capture.py worker --retry
```

Claude's actual context percentage can request a checkpoint once per compaction generation. Codex uses PreCompact because supported lifecycle inputs do not expose a trustworthy percentage. No context-window or auto-compaction threshold is changed.

## Data ownership and recovery

| Location | Contents |
|---|---|
| `memory/` | Private active Markdown notebook |
| `data/alignment-evidence/` | Immutable cited public evidence |
| `data/private/` | Backup key, saved renderer, and local state |
| `data/capture-queue/` | Durable capture metadata |
| `data/backups/` | Encrypted local restic repository |
| `runtime/` | Rebuildable catalogs, search assets, review checkpoints, dependencies |

All these local directories are Git-ignored. **Do not clean data/ as cache.** A backup on the same disk does not cover device loss; preserve the repository and its password separately on another device if needed. There is no automatic remote upload or pruning.

```sh
./am backup
./am backup-check --read-data
./am restore --snapshot '<explicit-restic-snapshot-id>'
```

Restore verifies data in isolated staging and refuses conflicting immutable files. Exact pinned evidence can be read by source/ID/hash even without the original transcript or derived catalog. Unpinned original content is not recoverable from the notebook alone.

## Notebook and review

See [review boundaries](docs/review.md), [architecture](docs/architecture.md), and [synthetic examples](examples/memory/MEMORY.md). Optional [zk](https://github.com/zk-org/zk) can index your local Markdown notebook; no modified zk binary is bundled. After installing zk, use `zk -W "$PWD/memory" index` and `zk -W "$PWD/memory" list --match "keyword" --quiet --no-pager`.

## Validation and limits

Local checks cover append/rewrite/no-op imports, coherent catalog/search publication, failure isolation, interrupted import retry, read-only evidence lookup, checkpoint boundaries, and real restic restore after loss of fixture originals and catalogs. A short live Codex CLI manual compact/resume cycle was validated during development, including a read-only lookup failure subsequently fixed. This does not establish broad production reliability or model alignment performance.

Prefix integrity verification still reads old bytes. New generation preparation still copies index assets. Changed sessions are retokenized as one document. Literal keyword search is not Korean morphological or semantic search. Native transcript schemas can change; unknown provenance is rejected/quarantined. Subagent and headless exec sessions are excluded from automatic alignment capture. The upstream candidate loader is capped at 100,000 sessions.

[MIT license](LICENSE) for original code/templates. See [third-party notices](THIRD_PARTY_NOTICES.md).
