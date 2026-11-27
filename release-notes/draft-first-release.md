# Releases are published only after their image assets are attached

## Changes for users

A GitHub release now starts as a draft. The container image, its digest file, the SBOM and the
vulnerability report are attached and attested while it is a draft, and the release is
published last, after a check that the assets, the digest and the tag commit belong together.
Release v0.43.2 was published before its assets could be attached (#659): it has no release
assets, its attestation steps were skipped, and its anonymous image availability and digest
were not established by that run.

## Upgrade

No action required. Take image digests from releases published after this change.

## Security

No security fixes in this release.
