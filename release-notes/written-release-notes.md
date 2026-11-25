# GitHub releases now open with a written summary

## Changes for users

Each GitHub release now begins with a written summary: what changed for users, what to do to
upgrade, and the security disposition. semantic-release's generated change list follows it,
collapsed. A release whose summary is missing or invalid now fails before any tag is created. A
read-only "Release notes preview" workflow shows the body a pull request would produce, and a
recovery command creates a missing GitHub release for an existing tag with its original body.
The server, its tools and its results are unchanged.

## Upgrade

No action for users of the server. Maintainers and contributors: every pull request that can
release (`feat`, `fix`, `perf` or a breaking `!` change) must add a fragment,
`release-notes/<slug>.md`, with the sections "Changes for users", "Upgrade" and "Security" (see
"Release notes" in CONTRIBUTING.md). If a tag exists but its GitHub release is missing, check
the tag out in a clean clone and run
`pdm run python -m scripts.release_notes recover <tag> --repository <owner/repo>`.

## Security

No security fixes in this release.
