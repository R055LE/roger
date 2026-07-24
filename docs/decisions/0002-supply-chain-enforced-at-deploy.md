# ADR-0002: Enforce supply-chain checks at the pull/deploy boundary

- **Status:** Accepted
- **Date:** 2026-07-23

## Context

Deployment is pull-based: the host polls `ghcr.io/.../roger:main` on a timer, **independent of the CI
release workflow's success**. So a vulnerability scan that runs *after* push, or a signature that's
only *created* at build, gates nothing — the host pulls `:main` regardless of whether the workflow
went green.

## Decision

Move enforcement to the boundary where the pull model actually decides what runs:

- Restructure release to **build (load locally) → Trivy scan → push**, so a failing scan blocks the
  publish entirely.
- Sign keyless with cosign at build **and** add a fail-closed `cosign verify` in the deploy script,
  between pull and `up`, pinned to the release workflow's OIDC identity.

## Consequences

- The scan and signature now actually gate what deploys, not just what CI reports.
- The deploy host hard-depends on cosign: fail-closed means no cosign, or a bad/absent signature,
  aborts the deploy — the last-good container keeps running (no outage, deploys pause until fixed).
- Two builds per release (load + cache-hit push); the second is a cache hit, so cheap.
- If the workflow filename or branch ever changes, the pinned verify identity must change with it.
