# Owned tool context and selected workspace

The attached session's built schema snapshot exposes an opaque context ID and schema revision. Main/runtime identity and profile isolation are distinct from a human effect or target grant. Discovery never rebuilds a frozen conversation schema or grants execution.

Changing the session's selected `cwd` now rotates the context ID alongside agent/profile changes. Returning to the old directory produces another new ID, so an old approval cannot revive through an A → B → A change. The path stays host-side; schema revision and the wire shape stay unchanged. The owned RPC checks this context before its durable handler claim and after dispatch. A new call with a retired context is refused before an attempt row or handler effect is created. Same-ID lookup of an existing attempt remains metadata-only.

After current-context admission, the private tool-only task inherits the owning conversation's backend overrides and selected nonempty working directory. Registration updates a cached environment's current directory as well as its raw scoped override. Authority is checked again before dispatch. Relative file targets therefore use the selected workspace instead of the gateway process directory. These operations use the existing routed home, secret and terminal scopes; credentials never cross the snapshot.

The regression uses a real file handler and durable SessionDB: stale context creates no attempt, a fresh relative path reads the selected target, returning to the original directory does not revive old authority, and the cached environment reads the original target only under a fresh context. The other profile receives no attempts.

This is a selected-working-directory fence, not a complete effect/target manifest. Backend/config/provider changes, explicit target resolution, resource integrity, grants for all operations, asynchronous effects, public/installed qualification and filesystem scope policy remain separately required. A handler return does not establish completion of every external effect; an already-dispatched effect is never retried merely because context changed.
