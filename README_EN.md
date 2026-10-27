# HeyTalk · Emotional Companion Agent

**English** | [简体中文](README.md)

> **Online portfolio:** <https://yrd-go.github.io/HeyTalk-Emotional-Companion/showcase/>
>
> GitHub Pages contains only the case study, screenshots and demo video. For the interactive
> walkthrough, the author runs the app on their laptop and opens `http://localhost:8600/app`.
> The static site does not host a chat backend or any model credentials.

HeyTalk is an emotional-companion prototype that treats safety constraints as part of the
product contract. Crisis signals are checked by deterministic rules before model generation,
high-risk turns use a fixed response path, and long-term memory is written only after user
confirmation.

![Desktop demo](showcase/assets/desktop-home.png)

## At a glance

| | |
|---|---|
| **Type** | Personal portfolio and small-scale prototype, not a commercial or clinical product |
| **Stack** | React 19 + Vite frontend; Starlette + SSE backend; Python rule layer |
| **Design priority** | Safety checks do not depend on the model; memory is explicit; fallbacks are visible |
| **Interactive demo** | Run on the author's laptop during the interview at `http://localhost:8600/app` |
| **Public case study** | [GitHub Pages](https://yrd-go.github.io/HeyTalk-Emotional-Companion/showcase/) |
| **Verified on** | 2026-10-26 |

> The phrase "I don't want to live anymore" in the demo is a **controlled safety-test input**,
> not a real user record. It demonstrates that a HIGH-risk message does not reach normal model generation.

---

## What was actually verified

The table below records commands and observations from the current audit. File presence alone is
not treated as proof that a feature works.

| Check | Result | Boundary |
|---|---|---|
| Automated suite | **753 tests, 0 failures, 0 errors, 1 skip** | Mostly Python, rule-layer and HTTP contract tests |
| Suite duration | about 281 seconds | Includes live weather and translation checks |
| Local `/app` | HTTP 200; rendered by Edge | The current browser session reached the login page |
| First-screen render | 2.435% non-white pixels; built assets matched source copy | Rendering passed; every logged-in interaction was not re-tested |
| `/api/bootstrap` | HTTP 200 | Page initialisation endpoint is reachable |
| `/api/icon` | HTTP 200, `image/png` | Icon resource is served |
| `/api/history` | HTTP 401 without login | Authentication is enabled; no user data was read |
| Global weather | Live New York lookup succeeded | Suite also covers Tokyo, London, Paris and Chinese cities |
| Translation | `你好，世界` → `Bonjour, le monde.` via MyMemory | Chinese to French; text is sent to an external service |
| Web search | 3 live result records returned | Result quality depends on the search engine |
| Import self-check | Passed | Optional LangChain modules are skipped when not installed |
| GitHub Pages | HTTP 200 | Static portfolio is reachable |

**Not claimed as verified in this audit:**

- A real microphone and complete Whisper transcription run.
- Every click and state transition after login. Browser evidence stops at the login page;
  the full interactive story is demonstrated locally.
- Superior answer quality for Pro, Flash or any other model.
- Long-term reliability of a Cloudflare Quick Tunnel. Its URL changes and it is not a production service.
- A clean-machine install from scratch. The local startup path is the one used by the author.

---

## Demo material

- [Desktop chat screenshot](showcase/assets/desktop-chat.png)
- [Mobile chat screenshot](showcase/assets/mobile-chat.png)
- [Demo video](showcase/media/heytalk-demo.mp4)
- [Interactive-looking case-study page](https://yrd-go.github.io/HeyTalk-Emotional-Companion/showcase/)

---

## Implemented and test-backed capabilities

### Safety gate

- HIGH / MEDIUM / LOW attention levels are decided by rules before generation.
- HIGH uses a fixed response path and real help resources; the model call count is zero.
- MEDIUM responses are checked for help-channel content.
- Tests: `tests/test_chat_safety.py`, `tests/test_server_api.py`.

### Consent-gated memory

- Candidate long-term facts are shown to the user instead of being written directly.
- Confirmed entries alone enter the profile; edit, reject and delete have separate paths.
- Tests redirect history/profile/feedback data to temporary directories.
- Tests: `tests/test_server_memory.py`, `tests/test_chat_profile.py`.

### Multi-user access

- Login, one-time invitations, token revocation and login rate limits are implemented.
- User histories are separated by account; unauthenticated history access returns 401.
- Tests: `tests/test_server_auth.py`, `tests/test_ratelimit.py`.

### Streaming chat and history APIs

- `/api/chat` streams through SSE; `/api/history` reads and deletes sessions.
- Tests use stand-in models to verify streaming completion, graceful degradation and API contracts.
- A real paid-model conversation was not re-run in this audit, so real-model end-to-end quality is not claimed.
- Tests: `tests/test_server_api.py`, `tests/test_chat_conversation.py`.

### Global weather

- Uses Open-Meteo and requires no API key.
- Live New York lookup passed; tests cover country-qualified names and namesake disambiguation.
- Network failures are reported as network failures rather than “city not found”.
- Tests: `tests/test_chat_weather.py`, `tests/test_chat_tools.py`.

### Translation

- Language resolution, script detection, Google / MyMemory fallback and a local cache are implemented.
- A live Chinese-to-French request returned `Bonjour, le monde.` from MyMemory.
- Tests: `tests/test_chat_translation.py`, `tests/test_chat_scaffold.py`.

### Web search

- The pipeline fetches result titles, snippets and URLs and formats them for a generation step.
- A live smoke test returned 3 records. The noisy results for one query show why this is not
  presented as a reliable search replacement.
- Tests: `tests/test_web_search.py`.

### Frontend

- React 19 + Vite is built and served by the Starlette app.
- Edge rendered `/app`; the server references assets that exist, source copy and the build agree,
  and the page is not blank.
- The current screenshot reached the login page. Button-level browser E2E coverage remains incomplete.

---

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cd web
npm ci
npm run build
cd ..

copy .env.example .env
# Fill only the credentials you actually use. Never commit .env.

python src\server\api.py
# Open http://localhost:8600/app
```

The author's machine currently runs this path. A clean-environment installation was not re-tested
during this audit.

---

## Tests

```powershell
python -m unittest discover -s tests -v
python scripts\import_check.py
```

Result on 2026-10-26 in the final public snapshot: `Ran 753 tests in 280.968s`, `OK (skipped=1)`.
There were no failures or errors; one external-network/subprocess test was skipped during the full run.
The translation (40), live weather (5), MCP probe (7) and CLI (5) groups were then rerun separately
and all passed. The import check also passed, with optional third-party modules skipped when absent.

The suite primarily covers rules, backend contracts and degradation paths. It does not prove that
the conversation is enjoyable or that the product is clinically effective.

---

## Privacy and security boundaries

- No `.env`, API keys, user conversations, profiles, feedback, invitation codes or session tokens
  belong in the public repository.
- The public repository contains source code, example configuration, demo material and curated notes.
- Weather, translation and search send necessary queries to third-party services.
- This is not a therapy tool. It does not diagnose, promise efficacy or replace professional care.
- This repository does not provide a hosted chat backend. Anyone deploying it separately needs HTTPS, authentication and access control.

---

## Known limitations

1. No real-user study; visual and conversational quality is supported by the demo and engineering tests, not clinical evidence.
2. No complete frontend E2E suite; the browser smoke check covers the login page and build consistency.
3. Search result quality is bounded by the upstream search page and can be noisy or blocked.
4. Real microphone capture and a full Whisper run were not re-verified in this audit.
5. Model answer quality was not blind-judged against frontier models.
6. No persistent public chat: GitHub Pages is a static portfolio; the interactive demo is run locally by the author.
7. RAG is not the main chat path; when vector retrieval is unavailable, the system falls back to local keyword retrieval.

---

## License

MIT. See [LICENSE](LICENSE).
