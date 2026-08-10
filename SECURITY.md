# Security and privacy

## Local-only data

Do not commit credentials, environment files, host paths, usernames, hardware fingerprints, VoiceBox profile metadata, reference audio, source media, downloaded video, generated output, job manifests, model weights, or runtime logs.

Runtime artifacts belong under the ignored `data/` tree. Python environments, third-party source, model caches, and agent-specific files are also ignored.

## Network boundary

The bridge and VoiceBox integrations must remain bound to loopback. Do not expose this prototype to a LAN or the public internet without authentication, CSRF protection, rate limiting, and a separate threat-model review.

## Credential handling

The application does not require cloud inference credentials or API keys. Never add a token to a URL, source file, test fixture, issue, log, or job manifest. If a credential is committed accidentally, revoke it immediately and rewrite the affected Git history before sharing the repository.

## Reporting

Report security concerns privately to the repository owner. Do not include real credentials, personal media, host paths, or VoiceBox profile data in a GitHub issue.
