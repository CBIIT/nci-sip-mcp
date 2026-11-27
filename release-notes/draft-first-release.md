# Releases are published only after their image assets are attached

## Changes for users

A GitHub release now starts as a draft. While it is a draft, the container image is pushed to
GHCR with its provenance and SBOM attestations, and the draft receives three release assets: the
vulnerability report `scan.json`, the SBOM `sbom.cdx.json` and the digest file
`image-digest.txt`. The release is published last, after a check that those assets, the image
digest and the tag commit belong together.
Release v0.43.2 was published before its assets could be attached (#659): it has no release
assets, its attestation steps were skipped, and its anonymous image availability and digest
were not established by that run.

## Upgrade

No action required. Take image digests from releases published after this change.

## Security

No security fixes in this release.
