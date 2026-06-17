# Security Policy

## Supported Versions

Security fixes are applied to the latest released version on the `main` branch.

| Version | Supported |
|---------|-----------|
| 1.0.x   | ✅        |
| < 1.0   | ❌        |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, use GitHub's private vulnerability reporting:

1. Open the **Security** tab of this repository.
2. Click **Report a vulnerability** to start a private advisory.

Please include the affected version, steps to reproduce, the impact, and any
suggested mitigation. Allow a reasonable time for a fix before any public
disclosure. We aim to acknowledge reports within a few days and to keep you
informed of remediation progress.

## Security Model

The server is the most exposed component of the HSYNC ecosystem. Relevant
controls:

- **Authentication** — all data endpoints require a Bearer API key
  (`HSYNC_API_KEY`), compared in constant time. WebSocket clients authenticate
  with an `auth` frame before any event is processed.
- **Transport security** — optional TLS (HTTPS/WSS); see the SSL configuration
  in the README.
- **Rate limiting** — per-IP limits (slowapi) on uploads, downloads, WebSocket
  connections and general endpoints, plus per-connection WebSocket message
  throttling. `X-Forwarded-For` is trusted only for `HSYNC_TRUSTED_PROXIES`.
- **Path-traversal protection** — bucket and filename validation reject unsafe
  paths.
- **Resource limits** — maximum upload size and minimum free-disk-space checks.

## Hardening Recommendations

- Always set a strong, unique `HSYNC_API_KEY`.
- Terminate TLS at the server or a trusted reverse proxy, and set
  `HSYNC_TRUSTED_PROXIES` accordingly.
- Keep `enable_docs` disabled in production (default).
