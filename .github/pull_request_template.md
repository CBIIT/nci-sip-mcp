<!-- The landing commit subject is the Conventional Commit title, such as `fix(evs): ...`.
Merge releases follow included commit messages; squash releases follow the title with a blank body. -->

## What changes

Closes #

## Release note

For a `feat`, `fix`, `perf` or breaking change: the added `release-notes/<slug>.md` fragment
(CONTRIBUTING.md, "Release notes"); otherwise "Not a releasing change".

## Acceptance outcomes

The `acceptance/expected/fixture.json` diff grouped by test function, with before and after counts,
or "No outcome moved".

## Review rounds

For a milestone pull request: the table of each round (finding, pass, fixed or rejected).
For a milestone pull request also: its waits by kind (active work, owner decision, external input,
review, CI), as recorded in the issues.
