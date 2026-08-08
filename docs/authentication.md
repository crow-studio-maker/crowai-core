# Authentication

Accounts use normalized lowercase email, unique lowercase usernames, and Werkzeug scrypt password hashes. Login failures are generic to resist account enumeration. Registration and login are rate-limited by both IP and account identity.

Sessions are stored in SQLite. Cookies contain only a cryptographically random opaque session ID and are HttpOnly, SameSite=Lax, scoped to `/`, and Secure in production. Session IDs and CSRF tokens rotate after successful login/registration. Logout clears state and revokes the previous server-side session record.

All state-changing API calls require an `X-CSRF-Token` matching the server-side session and pass same-origin validation. Credentials, tokens, and session IDs are never stored in localStorage or sessionStorage.
