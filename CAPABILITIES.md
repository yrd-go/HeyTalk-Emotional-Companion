# HeyTalk Capability & Verification Ledger

Last audited: **2026-10-26**

This file records what was actually verified, not what merely has source files. The public
repository is a static portfolio.

## Evidence labels

| Label | Meaning |
|---|---|
| **T** | Automated test passed in the full suite |
| **L** | Live external smoke test passed during this audit |
| **B** | Real browser rendered the page without a blank screen |
| **D** | Demonstrated in screenshots/video or by manual local use, but not re-clicked in this audit |
| **U** | Not verified in this audit; do not present as proven |

## Ledger

| Capability | Status | Evidence / boundary |
|---|---|---|
| Deterministic HIGH / MEDIUM / LOW safety gate | **T** | `tests/test_chat_safety.py` |
| HIGH risk never reaches model generation | **T** | `tests/test_server_api.py`; canary model records zero calls |
| MEDIUM risk reply keeps help resources | **T** | `tests/test_server_api.py` |
| Memory requires explicit confirmation | **T** | `tests/test_server_memory.py`, `tests/test_chat_profile.py` |
| Memory edit / reject / delete paths | **T** | `tests/test_server_memory.py` |
| Login, invitations and token revocation | **T** | `tests/test_server_auth.py` |
| Login rate limiting by account and source | **T** | `tests/test_ratelimit.py` |
| Unauthenticated history is rejected | **B/T** | Live `/api/history` returned HTTP 401 |
| SSE chat contract and history persistence | **T** | `tests/test_server_api.py`, `tests/test_chat_conversation.py` |
| Frontend capability wiring after the React migration | **T** | `tests/test_capabilities.py` |
| Optional review card and its routes | **T** | `tests/test_chat_review.py` |
| Real model conversation end-to-end | **U** | Stand-in models were used for deterministic API tests |
| Global weather and place disambiguation | **T/L** | Live New York query plus `tests/test_chat_weather.py` |
| Network failure is distinguished from unknown place | **T** | `tests/test_chat_weather.py` |
| Chinese-to-French live translation | **L** | MyMemory returned `Bonjour, le monde.` |
| Translation engine fallback and cache | **T** | `tests/test_chat_translation.py` |
| Live web-search fetch | **L** | Returned 3 result records during this audit |
| Search parsing and network-failure fallback | **T** | `tests/test_web_search.py` |
| Current local page renders | **B** | Edge loaded `/app`; 2.435% non-white pixels |
| Build assets and source copy agree | **B** | `scripts/smoke_frontend.py` passed |
| Login page visual state | **B/D** | Real browser screenshot reached the login form |
| Logged-in chat UI click path | **U** | Requires a local account; not re-clicked in this audit |
| Voice endpoint validation and cleanup | **T** | `tests/test_transcribe.py` |
| Real microphone + complete Whisper transcription | **U** | Not run in this audit |
| Audio playback / read-aloud controls | **D** | Present in demo material; no automated browser assertion |
| Thinking stream and message actions | **D** | Present in demo material; no automated browser assertion |
| Desktop and mobile layouts | **D** | Screenshots and video; no full viewport E2E suite |
| Local keyword fallback when vector retrieval is unavailable | **T** | `tests/test_retrieval_fallback.py` |
| Persistent public chat backend | **U** | Not offered by design; the portfolio is static |
| Model quality superiority | **U** | No blind evaluation against frontier models |

## Full-suite result

On 2026-10-26:

```text
Ran 753 tests in 280.968s
OK (skipped=1)
```

No failures or errors were reported. One external-network/subprocess test skipped during the full
run; the translation (40), live weather (5), MCP probe (7) and CLI (5) groups were rerun separately
and all passed. The suite is primarily Python and backend/rule-layer testing; it does not prove
visual polish, usability or clinical effectiveness.

## Product boundaries

- Not a therapy or diagnostic product.
- Not a high-concurrency production service.
- Not a claim that a language model is safer than deterministic rules.
- Not a promise that search results or model answers remain available; no public chat backend is offered.
- Demo inputs that mention self-harm are controlled safety-gate tests, not real user records.
