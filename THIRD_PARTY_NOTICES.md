# Third-party components

- **aichat-search**, part of [claude-code-tools](https://github.com/pchalasani/claude-code-tools), pinned to commit `0ca732006a387dfe38a601a4959197ce0df778d8`, crate 0.3.1, MIT. Copyright notice and license are retained in `third_party/aichat-search/LICENSE`. The small `--index-path` patch is included separately. Source and binaries are downloaded/built locally, not bundled in this repository.
- **Tantivy Python bindings** 0.25.1: installed separately in the local indexing environment. The upstream Rust engine uses Tantivy 0.25, pinned through its Cargo.lock.
- **restic**: external backup executable. [Upstream](https://github.com/restic/restic), BSD-2-Clause. No restic source or binary is redistributed here.
- **zk**: optional Markdown notebook CLI. [Upstream](https://github.com/zk-org/zk), GPL-3.0. It is not bundled or modified by this repository.

PersonaGraph's original adapters and synthetic templates are MIT licensed. External components retain their respective licenses.
