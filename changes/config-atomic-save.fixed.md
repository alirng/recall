**config.toml is written in one step**, a temp file then a rename, so a hook reading the settings while they're saved sees the old file or the new one, never half of it.
