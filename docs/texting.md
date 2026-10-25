# Texting the agent

You text a request to the agent's iMessage number. A Claude agent does the research, spending through Sentinel under the same rules as the MCP path, and texts back a briefing with sources.

```
your phone ──iMessage──► Photon line (Spectrum Cloud, free shared pool)
                              │  gRPC stream
                              ▼
                         texting_relay (bun, spectrum-ts)
                              │  POST /texting/inbound   GET /texting/outbox (long poll)
                              ▼
                         Sentinel (python -m sentinel.mcp_server)
                              │  texting.TextDesk: link check, agent loop, approvals
                              ▼
                         guard + intent review + wallet + books (unchanged)
```

## What is different from the MCP path

- **No web access.** The agent's only tools are `start_task`, `find_services`, `quote`, `pay` and `resume_payment`. It has no web search or fetch tool, and its instructions say to cite a Sentinel source for every fact and to say what the sources didn't cover.
- **The task is your words, verbatim.** `start_task` takes no text from the model. The mandate is built from the texts you sent since the last task.
- **Approvals come as links.** For a seller Sentinel hasn't seen, you get "Approve $0.40 to host? <link>". The link opens `/account/approvals/<id>`, the same authenticated page as before. When you tap Yes there, the agent continues by itself. A payment held by intent review or by escrow texts you a link to `/account/tasks`. A reply text can never approve anything.

## Set up (local)

1. Sign up at [app.photon.codes](https://app.photon.codes), create a project, enable Spectrum and iMessage, and note the project id and secret. The free shared line pool needs no subscription.
2. In the repo's `.env`, set:
   - `SENTINEL_TEXTING_SECRET`: 24+ characters, for example from `python -c "import secrets; print(secrets.token_urlsafe(32))"`
   - `SPECTRUM_PROJECT_ID`
   - `SPECTRUM_PROJECT_SECRET`

   Then run `python -m sentinel.db migrate` (migration 016).
3. Start Sentinel: `python -m sentinel.mcp_server`. The log says `texting is on`.
4. Start the relay:
   ```
   cd texting_relay && bun install && bun run start
   ```
   Set `SENTINEL_URL` if Sentinel is not at http://localhost:8000.
5. Sign in at `/account/texting` and tap **Get a code**. Text the code (for example `K7Q2-9MZD`) from your phone to your Photon line. It replies "Linked."
6. Text a request, for example: *I'm meeting Acme Robotics next week. Pull their funding history, headcount trend, recent news, and any litigation. Use paid sources only if the free ones don't cover it. Budget $1.*

Text commands: `RESET` starts a fresh conversation, and `STOP` unlinks the number. **Sign out everywhere** also unlinks it.

## Limits

- **Free shared lines.** Different people may see different sending numbers, but each conversation keeps its own. A line can only text someone who texted it first, so the relay answers in the chat the person opened. Photon caps the free tier at 5,000 messages a day and 50 new conversations per line per day.
- **Restarts.** Conversations are held in memory per number for 6 hours, and a restart starts them fresh. Tasks, payments and books are unaffected. If the relay restarts, it forgets open chats until each person texts again.
- **One request at a time.** One request runs per number. A text sent while one is running waits its turn.
- **Approval expiry.** Approval links expire after 5 minutes, as on the MCP path.
- **1:1 only.** Group chats are ignored.
