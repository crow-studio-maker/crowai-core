# Uploads

Uploads support both file-picker and drag-and-drop interaction. The server enforces ownership, count and byte limits, safe filenames, random stored names, atomic writes, SHA-256 hashing, basic MIME sniffing, executable rejection, archive entry/expansion/compression limits, and non-executing local inspection.

The browser receives sanitized metadata only. `stored_path`, `local_path`, and other filesystem keys are removed recursively. Upload IDs can be attached only by their owner.

`X-Idempotency-Key` or multipart `request_id` may be used to replay a completed upload response after a retry. High-risk deployments should add external malware scanning and separate storage permissions.
