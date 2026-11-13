- **Agent workers on the S3 workspace backend start again.** A worker hydrating its mounts through the
  gateway's `/agent/workspace-store` sent its delegation token as `Authorization: Bearer`, which the
  gateway reads only on `/mcp`; every boot ended in `401 Missing API key`, so a chat stayed on
  "Starting Agent" and no meeting bot was sent. The store client now sends the token in `X-API-Key`,
  like every other worker call to the gateway. Only the agent-worker image changes.
