# The AISMM story

Notes for blog posts and social updates about building **AISMM** — an autonomous agent that
researches, creates and publishes social content on a schedule. Newest first.

---

## 2026-09-21 — The agent can read my private repos now

**What shipped:** a *Git connection* in Settings. Paste a GitHub token, pick it on an instruction,
and the agent gets six read-only tools: list repos, recent commits, one commit in detail, compare two
points, read a file, list pull requests. The build-in-public posts it already wrote about my public
repos now work for the private ones too.

**Why it mattered:** I'd been asking the agent to watch my commits and post about what I'm building.
That worked on public repos because it could just open github.com in its browser. On a private repo
the browser gets a 404 — there's nothing to scrape.

**The interesting decisions:**

- **No default token.** Image and video connections fall back to a deployment-wide key. Git doesn't.
  A token that can read private code should be handed to specific instructions, not inherited by
  every run.
- **Read-only by construction.** Every call is a `GET`. There's no write path, so there's no write
  bug.
- **The agent names repos, never URLs.** The token only ever goes to the API URL you configured, so
  nothing the model types can send it somewhere else.
- **"Remember where you got to" is just memory.** The agent saves the newest commit sha after
  posting and asks for commits *since* it next time — the same trick it uses to walk through a comic
  one panel per day.
- **A 404 isn't "doesn't exist".** GitHub answers 404 for a private repo your token can't see, so the
  tool says "missing *or* not visible to this token" instead of sending the agent off in the wrong
  direction.

**A small gotcha worth a post on its own:** one tool's description vanished. The Agents SDK parses
docstrings with a style auto-detector, and "One commit in detail: full message, …" looked like a
section header to it. The model would have seen a tool described only by its privacy warning. Fix:
pass the description explicitly instead of trusting the docstring.

---

## 2026-09-20 — "Event loop is closed": the bug a dependency upgrade woke up

**Symptom:** after upgrading the OpenAI SDK, every scheduled run *except the first one after a
restart* died about a second in, at the very first model call. Four dead runs overnight.

**Cause:** each run is its own `asyncio.run()`, which closes its event loop at the end. The HTTP
client was cached for the whole process, so the next run got a connection pool full of sockets that
belonged to a loop that no longer existed. Closing one of them raised `RuntimeError: Event loop is
closed`.

**Why it showed up now:** that cache had been wrong for months without anyone noticing. The new SDK
pulled in a newer HTTP stack that *reports* the error instead of swallowing it. The upgrade didn't
add the bug; it revealed it.

**Fix:** cache clients per event loop and close them when the loop ends. Reproduced first in six
lines (one client, two `asyncio.run` calls → the second fails), then fixed: five runs in a row, all
fine.

**Takeaway for the post:** "the first run works, every run after it fails" is a signature. It
almost always means state is leaking from one run into the next.
