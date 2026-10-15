# Changelog fragments

A PR adds one file here and does not edit CHANGELOG.md: `changes/<topic>.<section>.md`, where the
section is `upgrading`, `added`, `changed` or `fixed` (for example `changes/batch-api.added.md`).
The file holds one bullet's text without the leading `- `; start with a bold lead-in when it helps. No em dashes.
`python tools/changelog.py --check` validates the fragments and `--preview` prints the section a release would add.
At release, `python tools/changelog.py --release X.Y.Z --date YYYY-MM-DD` compiles them into CHANGELOG.md and deletes them.
