# Implementation status against `plan.md`

Status vocabulary is fixed: **Implemented**, **Partial**, **Unchanged Upstream**, **Deferred**, **Blocked**. “Implemented” means the stated local/deterministic behavior has direct evidence; mocks or scripted inference never satisfy a live exit criterion.

| Phase / criterion | Status | Direct evidence | Exact remaining work |
|---|---|---|---|
| 1.1 pin Strix revision | Implemented | `verify_runtime_compatibility`; `verify_strix_source`; `tests/test_compatibility.py` | Re-pin only through the documented upgrade procedure. |
| 1.2 install pinned SDK | Implemented | locked sync/build validation; `pyproject.toml`, `uv.lock` | Other Python/platform combinations remain untested. |
| 1.3–1.6 Team auth, org `/status`, no API override, live query | Implemented | Official status reported first-party `claude.ai`, an active organization (identity withheld), and `team`; override selectors were unset; live query completed | Bridge-side organization enforcement and written policy approval remain separate blockers. |
| 1.7 live custom MCP | Implemented | Claude selected strict `sandbox_exec`; final full fixture scan selected 19 real Strix tool calls | Exhaustive browser/Caido/web tool autonomy remains unverified. |
| 1.8 limits/model/headless | Partial | Headless default selected `claude-opus-5[1m]`; usage/rate frames observed; two concurrent probes completed | Maximum quota/concurrency, reset behavior, stable latency, and future model availability remain unknown. |
| 1.9 live cancellation/timeout | Implemented | Programmatic live interruption disconnected/cleaned up; live 0.5-second MCP timeout recovered and cleaned up | Whole-process hard deadlines and rollback of completed effects remain unproved. |
| Phase 1 exit criteria | Implemented | Team-backed Python session, strict custom MCP call/result, streaming/model/usage/error evidence; see [live verification](live-verification.md) | Policy approval and broader product claims are not Phase 1 technical evidence. |
| 2.1–2.4 isolated adapter, prompt, minimal tools, Docker routing | Implemented | Deterministic tests plus live Strix/Docker fixture execution | Exhaustive live tool/model behavior remains unknown. |
| 2.5–2.6 authorized Claude task and capture | Implemented | Team-backed fixture inspection produced one finding and full artifacts | Arbitrary-target quality is unclaimed. |
| Phase 2 mounted inspection / sandbox / no Runner | Implemented | Claude inspected the mounted fixture, used Strix sandbox tools, and completed without `Runner.run_streamed` | Bind-mounted targets are writable; operators must use disposable copies. |
| Phase 3 schema/context/result/error/timeout/cancel bridge | Implemented | `tests/test_tool_adapter.py`; generated [inventory](tool-inventory.json) | Live provider selection and exhaustive tool behavior. |
| 3 effective root tool parity | Partial | Root/child 33-tool inventories match pinned runtime | Browser/Caido/web/search behavior not exhaustively live-tested. |
| 3 reuse existing tools | Implemented | `_effective_agent_and_tools`; ownership table | Upstream public backend-neutral tool seam preferred. |
| Phase 4 execution abstraction methods | Implemented | `backend.py`, `claude_backend.py`; deterministic tests; live init/stream/tool/rate/result frame sample | Exhaustive frame variants and live reconnect remain unverified. |
| 4 backend selection in native Strix | Deferred | Companion `strix-claude-bridge` only | Add upstream Strix backend dispatch/configuration. |
| 4 existing providers unchanged | Unchanged Upstream | No Strix source patch; companion does not invoke existing path | Upstream regression suite needed for native integration. |
| 4 complete live single-agent scan | Partial | A complete live root/child companion scan passed | Dedicated single-agent-only evidence and native Strix backend selection remain absent. |
| Phase 5 per-agent sessions / graph tools / notifications / controls | Implemented | Deterministic suite plus live root/child graph, autonomous tool choices, finding and completion | Maximum account concurrency remains unknown. |
| 5 interrupted agents resume queued messages | Partial | Live root wait returned `aborted_tools`; bounded same-process continuation reached `finish_scan` | Atomic restart graph/mailbox restore plus provider tool-use identity. |
| Phase 5 exit criteria | Implemented | Live provider root/child scan completed with one finding and exact-one lifecycle completion | This does not satisfy deferred restart or native integration criteria. |
| Phase 6 event translation | Partial | `tests/test_event_adapter.py`; [event mapping](events-sessions-resume.md) | Full Go TUI/local-viewer frames absent. |
| 6 session ID metadata | Partial | raw ID is memory-only; owner-only state stores SHA-256 audit hash and checkpoint metadata | Plan asks to store the SDK ID, but durable consumable authority is intentionally withheld while restart is unsafe; live shape unverified. |
| 6 restart resume / duplicate prevention | Deferred | CLI rejects before side effects; metadata-only journal tests | Full safe restart contract and process test. |
| 6 SQLite authority decision | Partial | SDK session is inference authority; JSONL is mirror | Native Strix SQLite/viewer integration remains upstream work. |
| Phase 6 exit criteria | Deferred | Neither live interface parity nor restart exit criterion is met | Implement both before claiming completion. |
| Phase 7 request/token/cache/model accounting | Implemented | Deterministic tests plus live first-party model/input/output/cache fields | Provider fields/model availability can change; absent model remains intentionally unknown. |
| 7 auth mode/zero dollars/no API budget | Implemented | state/report assertions | Live subscription quota semantics unverified. |
| 7 turns/runtime/concurrency/tools/rate errors | Implemented | backend/CLI/multi-agent tests | Live service limit variants and SDK enforcement unverified. |
| Phase 7 exit | Partial | Host controls plus live model/token/cache/rate/concurrency observations | Maximum limits, reset behavior, and stable model availability remain unknown. |
| Phase 8 authorization / Docker confinement / no credential mounting | Implemented | CLI/auth/Docker tests and live sandbox execution | Bind-mounted target changes can reach the host; use a disposable copy. Production remains non-multi-tenant. |
| 8 no secret/full traffic logs | Implemented | omission/sentinel tests plus repaired live rate-event identifier leak | Explicit sensitive stdout opt-in still carries operator risk. |
| 8 approved organization | Partial | Official status identified an active Team organization; identity withheld | Bridge cannot enforce organization selection; cyber/policy approval remains operator/external. |
| 8 user-run/no hosted login/pooling | Implemented | architecture and auth boundary; no hosted code | Publication still blocked. |
| 8 data/telemetry/policy docs | Implemented | README, security, ownership, operations | Current terms must be manually revalidated. |
| Phase 8 exit | Blocked | Technical controls pass; policy/org do not | User-owned approvals. |
| Phase 9 unit areas | Implemented | deterministic suite covers every listed unit area; resume metadata is rejection/audit only | Safe resume functionality intentionally absent. |
| 9 local single/root-child/tool failure/rate/cancel/report/SARIF | Partial | Live root-child/report/SARIF, live rate metadata/cancel/timeout probes, deterministic failure paths | Not every failure/pressure path has live evidence. |
| 9 context-window pressure | Partial | scripted `context_window_exceeded` mapping/cleanup only | Real context pressure scenario. |
| 9 restart after process | Deferred | rejection test only | Safe implementation plus true new-process test. |
| 9 security fixtures | Implemented | bundled purpose-built vulnerable app only | Optional approved local Juice Shop/DVWA/WebGoat expansion. |
| Phase 9 exit | Partial | Credential-free ladder and one full live fixture scan pass | Real pressure, restart, full interface, and platform evidence remain. |

## Initial deliverables

| Deliverable | Status |
|---|---|
| Architecture decision record | Implemented: six ADRs in [decisions](decisions/README.md). |
| Authentication/MCP spike | Implemented for the authorized Team technical probe; policy approval remains external. |
| Single-agent prototype | Partial: live combined root/child companion path passed; dedicated single-only/native path absent. |
| Tool bridge with tests | Implemented for pinned deterministic scope. |
| Multi-agent backend | Partial: deterministic and live companion paths pass; native Strix selection absent. |
| Event/resume compatibility | Deferred: limited event projection; restart disabled. |
| Usage-limit controls | Implemented locally with basic live observations; maximum service semantics unknown. |
| Security/deployment documentation | Implemented; policy approvals remain user-owned. |
