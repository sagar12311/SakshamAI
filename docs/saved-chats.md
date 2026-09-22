# Saved conversations

The public gateway stores projects, chat metadata, and message turns in its
existing PostgreSQL database. With the current Compose setup, this is the
`gateway-postgres` named Docker volume on the gateway host (the Mac), not the GPU
machine or Supabase. Supabase verifies identity; the GPU receives only the model
context over the existing private connection. The Mac and tunnel must be running
to retrieve history. Docker restarts retain the volume; deleting the volume loses
the database. Back up PostgreSQL before moving hosts; backups must be managed
separately from deletions in the live application database.

The idempotent `app/history.sql` migration runs before gateway readiness. Deploy
the gateway before the frontend. Rollback to the earlier gateway preserves the
new tables, but saved-history endpoints will be unavailable until upgraded again.
The gateway runs one process: startup marks interrupted generation as failed so
users can retry. Do not deploy multiple gateway replicas with this recovery rule.

Every history endpoint derives ownership from the verified session. Projects and
conversations use authenticated CRUD routes under `/v1/projects` and
`/v1/conversations`; sending uses `POST /v1/conversations/{id}/messages` with a
client-generated conversation UUID and request UUID. Message retries reuse the
request UUID. Replies serialize per conversation. Listing uses limit/offset;
message history uses a `before` turn-ID cursor and returns oldest-to-newest turns.

The first submitted message creates the conversation and supplies its title.
Pinned state and project membership are independent. All conversations appear in
Recents. Only the latest fourteen completed exchanges plus the current message
are included in inference; older and failed messages remain available in history.
API keys remain in memory and must be reentered after reopening a BYOK chat.

Run the integration tests using `HISTORY_TEST_DATABASE_URL` and
`PYTHONPATH=gateway pytest gateway/tests`. Tests create and remove a uniquely named
schema and never use production chat tables. CI provides disposable PostgreSQL.
