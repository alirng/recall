## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## Checks

- [ ] `uvx ruff@0.16.10 format src && uvx ruff@0.16.10 check src` passes
- [ ] I ran the commands this change touches end to end (and the MCP server or hook, if affected)
- [ ] For prompt or extraction changes: I bumped the prompt version and compared `recall eval memory --fresh` averages before and after
- [ ] README or CHANGELOG updated if behaviour changed
- [ ] No real chat content, memory notes, keys or personal paths in the diff, fixtures or description
