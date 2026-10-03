# PersonaGraph workflows / 동작 흐름

[Open in the browser / 브라우저에서 보기](https://fmsongx2.github.io/personagraph/)

- [Startup and context routing / 시작과 맥락 읽기](startup.html)
- [Capture, verified evidence and manual alignment / 수집·근거·수동 갱신](evidence.html)

The self-contained HTML files can also be downloaded and opened offline. GitHub file previews show HTML source; use the browser link above to explore the viewers. Authored labels are Korean; built-in viewer controls use English. These are static source-backed explanations, not live telemetry or queries into personal memory.

## Evidence and validation

Both workflow specifications pin repository commit `1f0372f7e2ad0f9252b76e7dcded1d76162bb88a` and link nodes to inspected source ranges. Later README changes do not change that source snapshot. [checks.json](checks.json) records artifact/specification hashes and the automated validation, delivery, strict artifact check, and real-browser check gates. Full local receipts and screenshots are excluded because they contain local paths.

## Regeneration

Use [Archify](https://github.com/tt-a1i/archify) 3.0.1. It is not required to run PersonaGraph. The JSON files are authored sources; the `meta.output` fields name the original `.archify/` authoring folder relative to the repository. From the repository root, run `finalize workflow` on each JSON with `--repo-root . --quality showcase --json`, setting the HTML output to its matching `meta.output`. Copy the passing HTML to this folder, preserve its bytes, and update checks.json. Keep execution sequential for outputs sharing a directory. Authoring and viewer license: [MIT](../../third_party/archify/LICENSE).

The automatic capture hook does not create active Markdown policy. An agent reviews original evidence and records useful user decisions separately. No-change or deferred reviews do not require a new node.
