## PR pipeline

```
opened ──▶ review ──▶ lgtm + CI green ──▶ merged
           [needs-human]   [human approves]
```

## What does this change?

<!-- Required: one sentence -->

## Why?

<!-- Link the issue this closes: "Closes #NNN" -->
Closes #

## Verification

<!-- Required: describe what you built and tested -->
- [ ] ISO built: `just iso-sd-boot <target>` completed successfully
- [ ] Installer/ISO behavior change: `just debug=1 plain-e2e <target>` completed and the installed system booted
- [ ] Live boot smoke: `just boot-iso-serial <target>` showed `DAKOTA_LIVE_READY` (optional; does not prove installation)
- [ ] Container-only change (no ISO boot required — state reason)

## Checklist

- [ ] PR title follows Conventional Commits (`fix:`, `feat:`, `ci:`, `docs:`, etc.)
- [ ] Skill file updated in this same PR (if a pattern or lesson was discovered)
- [ ] Both AI attribution trailers present on every AI-authored commit (if applicable)
- [ ] I am using an agent and I take responsibility for this PR (if applicable)
