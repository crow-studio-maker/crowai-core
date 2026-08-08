# Conversations and durable memory

Conversation ownership is enforced by `owner_key`; IDs alone never grant access. A conversation is bound to the model selected at creation and cannot silently switch models later.

Creation supports a client request key through `request_id`. Ask requests also accept `request_id`; completed responses are replayed rather than duplicating assistant messages after a browser retry. In addition to idempotency, Core holds one operation-wide processing lease per conversation, so a refreshed tab or a second request key cannot start another turn while the first is generating. Long turns renew that lease periodically; if the process crashes, the lease eventually expires and can be reclaimed. `GET /api/conversations/<id>` exposes only the safe active/inactive processing state, allowing a refreshed UI to restore the “Thinking…” indicator and keep prompt/upload/model/send controls locked until the stored result arrives. Completion/release operations remain scoped to owner + request key + operation. Old completed and expired processing entries are periodically pruned without a full cleanup on every request.

## Memory

SQLite remains authoritative. Migration `0004_conversation_memory.py` creates one bounded memory row per conversation with an `ON DELETE CASCADE` foreign key. Every model request receives:

- bounded newest raw messages;
- a durable summary;
- structured relevant facts with source-message provenance;
- compact mode state; and
- the opaque conversation ID.

Memory is updated only after both the user and assistant turns have been persisted. A memory-update failure is logged but does not roll back/corrupt the authoritative conversation. Direct user corrections supersede older remembered facts. Size bounds prevent the memory prompt from growing without limit, and owner checks prevent reading another owner's memory.

Deleting an actively generating conversation first keeps that conversation's composer locked, marks the Core turn cancelled and invokes the loaded bound package's cancellation callback, which terminates package-owned active llama.cpp inference without waiting for the blocking HTTP request lock. Core then waits a bounded interval for that exact request thread to unwind before deleting SQLite state, so a racing final model payload cannot be persisted during deletion. If the request does not stop within the bound, deletion fails closed with a conflict instead of racing the live writer; the browser re-reads the persistent processing state before re-enabling input. Inactive conversation deletion does not kill a shared idle/backend process unnecessarily. Core then releases the processing lease and deletes the authoritative SQLite conversation. This prevents a deleted Code/Chat/Agent turn from continuing to hold package-owned GPU work in the background. After deletion, `delete_conversation(conversation_id=...)` performs mode-specific cleanup; Agent V1.0 removes its session state from the private Core-owned model-state database. “Clear my local data” iterates through owned conversations using the same deletion path and then clears uploads/snapshots, so conversation-specific Core and model state is removed as well.

User/assistant messages are committed to SQLite. If a process stops after a user message is written, that incomplete conversation remains visible after restart. Signed-in users receive rebuildable JSON snapshots; snapshots are never the memory authority.
