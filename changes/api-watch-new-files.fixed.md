**`recall api` no longer misses new chats when it starts cold.** Its change watcher watched only the
files that existed when a client subscribed, and `recall.db-wal` exists only while something has the
index open, so when no agent had the index open the Mac app could miss new chats: its lists didn't
update and a search didn't show the chats its scan added. It now starts watching a file when it
appears.
