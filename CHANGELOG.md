# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2025-12-xx

### Added
- Initial release: REST API for file upload/download, bucket management
- WebSocket real-time event broadcast
- API Key authentication (Bearer Token)
- SHA-256-based file integrity verification
- Integrated Prometheus metrics
- Docker-ready deployment with multi-arch support (amd64, arm64)
- Rate limiting per endpoint and per IP
- Symbolic link synchronization support
- Per-bucket connection management
- Resumable uploads with offset tracking
- Deletion tombstones (`.hsynctombstones`)
- Clock-synchronization endpoint (`/api/v1/time`)
- Zstandard (zstd) transfer compression
- `docker.sh` management script
- Comprehensive documentation

[1.0.0]: https://github.com/albertobasso/hsync-server/releases/tag/v1.0.0
