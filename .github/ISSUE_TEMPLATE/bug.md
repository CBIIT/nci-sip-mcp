---
name: Bug
about: The server, the suite or a document does something other than it says
labels: bug
---

## Impact and desired outcome

What task is affected, and what should the caller or maintainer be able to do?

## What happened

The command or tool call, its arguments, and what it returned (the error record with its
`correlationId`, if there is one). Preserve the first failure and link exact logs or evidence;
label a suspected cause separately from what was observed. No credentials or licence keys.

## What was expected

What should have happened, and where that is stated: the requirement id in `spec/`, the document,
or the upstream response. Define unfamiliar operation or gate codes.

## Version

The release tag or commit (`git describe --tags`), and fixture or live mode.

## Dependencies and decisions

Known blockers, related issues and any decision needed before a fix. Link existing approval or
hold records; a proposed fix is not approval. Omit this section if none apply.

## Completion evidence

What caller-visible result would demonstrate the fix? Keep the original failure separate from later
attempts; a passing rerun alone does not establish a cause or correction.
