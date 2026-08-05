# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- REST endpoints now log the originating client ID: `[UPLOAD]` and
  `[DELETE]` log lines include `(CID: <cid>)` (the CID was already a form
  parameter), and the `[DOWNLOAD]` endpoint accepts an optional `cid` query
  parameter (sent by the client) which is included in the log line. The
  OpenAPI snapshot has been regenerated to reflect the new query parameter.
