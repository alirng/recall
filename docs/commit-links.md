# Which chat wrote this

`recall why` follows a file, a line or a commit back to the chats that produced it: git blame
finds the commit, and recall names the chats whose agent wrote its lines or ran the commit.

```text
$ recall why src/auth/session.py:42
src/auth/session.py:42  (github.com/acme/harbor)

3f9c2a1e  2026-09-14  Keep the redirect target through login  (1 line here)
  Claude Code · 2026-09-14 · Fix the login redirect
    made this commit and wrote 38 of its 41 lines · claude:5b1e…
    asked: "after login it lands on the home page, it should go back where you were"
```

`recall why --commit <sha>` does the same for a commit, and agents ask through MCP
(`recent_chats` with `file` or `commit`). recall matches the lines each chat's agent wrote
(Edit, Write, apply_patch) with the lines each later commit adds, so rebases, squash merges
and clones of the repo need nothing special. A file changed only through a shell command is
not seen.

The links never leave your machine. They live only in recall's index, and recall uses git
read-only (`rev-parse`, `rev-list`, `log`, `show`, `blame`, with optional locks, lazy fetching
and textconv off): nothing goes into a repo. That rules out commit messages,
trailers, git notes, refs, branches, hooks and objects, and nothing is pushed or fetched, so
no chat id reaches a project or its collaborators. Agents are told to use what `recent_chats`
returns to answer you, never to copy it into commits, PRs or files. Links are worked out
without a model, during the nightly run and on each `recall why`. What recall reads from
transcripts for them is kept after an agent deletes a transcript, since it cannot be read
again. Set `[links] enabled = false` in `config.toml` to stop linking; the next nightly run (or
`recall links`) then deletes all of it.
