# Roadmap

This document outlines the planned direction for HSYNC Server. Items are
organized by status and are not commitments — priorities may shift based on
community feedback and contributor availability.

## In Progress

- **Desktop GUI** — [hsync-desktop](https://github.com/albertosdk/hsync-desktop), a cross-platform graphical front-end
- **Android app** — [hsync-android](https://github.com/albertosdk/hsync-android), native file sync for Android

## Planned

- **Web admin UI** — browser-based dashboard for server management (bucket overview, connected clients, metrics)
- **S3-compatible storage backend** — store buckets on S3/MinIO instead of local filesystem
- **File versioning** — keep N previous versions of each file for point-in-time recovery
- **Webhook notifications** — POST events to external services on file changes
- **Per-bucket API keys** — fine-grained access control with separate keys per bucket
- **IPv6 support** — full dual-stack operation
- **Metrics dashboard templates** — pre-built Grafana dashboards for server monitoring

## Considering

- **FUSE mount** — mount a bucket as a local filesystem (read-only)
- **Built-in Let's Encrypt integration** — automatic TLS without a reverse proxy
- **Multi-server replication** — replicate buckets across multiple servers for HA
- **gRPC API** — alternative to REST+WebSocket for high-throughput environments
- **Plugin system** — custom storage backends, auth providers, event hooks

## Completed

- [x] REST API for file upload/download/delete
- [x] WebSocket real-time event broadcast
- [x] API Key authentication
- [x] SHA-256 integrity verification
- [x] Docker deployment with multi-arch images
- [x] Rate limiting
- [x] Resumable uploads
- [x] Deletion tombstones
- [x] Zstandard compression
- [x] Prometheus metrics
- [x] Comprehensive documentation
