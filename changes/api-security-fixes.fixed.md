**A config.toml key with a space no longer breaks the file.** Saving settings quotes every key, writes nested tables properly, checks the text reads back as what was saved before replacing the file (and refuses otherwise), and creates a new config.toml readable only by you.

**recall api no longer repeats an API key in an error, and hides crash details.** An unknown key reference is reported without its text, every error message goes through the credential redaction the indexer uses, and an unexpected failure answers with a generic message while the detail goes to api.log.

**Switching from a private model to one that sends excerpts away needs your consent.** `PUT /v1/model` answers `409 needs_consent` with the destination unless the body repeats it as `accept_destination`.

**Edits and forgets must carry the note's stamp**, and a forgotten chat no longer shows in the API's chat list, messages, search results or counts. One malformed note no longer breaks every notes endpoint: it is skipped and logged. Restore only puts back a note that was forgotten, `PUT /v1/coordination` answers 400 for values of the wrong type, and `recall api` refuses to replace a file or link at its socket path.

**The Mac app is built for the installed recall by default.** The checkout is baked in only with `RECALL_DEV_PROJECT=/path`. It also refuses malformed or oversized answers from recall api, compares real paths in the scratch-copy guard of its write probes, runs Show Changes without a repo's diff drivers and keeps the diff private, and ⌫ and ⌘Z in Memory act on the highlighted note and only in that section. The Forget sheet says when an unsaved edit goes with the note. build.sh pins its font tools and fails if signing fails.
