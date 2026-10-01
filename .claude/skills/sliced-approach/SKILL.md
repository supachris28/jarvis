---
name: sliced-approach
description: Break substantial work into small, reviewable slices with clear outcomes and targeted user questions when needed. Use when a task has multiple separable outcomes or the user asks to work incrementally.
---

# Sliced approach

Deliver substantial work in small increments that each have a clear outcome and are easy for the user to inspect. Keep the user's overall goal in view while limiting each slice to a coherent, reviewable change.

## Workflow

1. **Understand the goal.** Identify the desired end state, constraints, and what would count as a useful first outcome. Inspect the relevant workspace before proposing implementation details.
2. **Clarify only material uncertainty.** Ask concise questions when an unanswered choice would change scope, behavior, or the shape of the deliverable. Continue independent discovery and preparation while waiting. If the uncertainty is minor, state a reasonable assumption and proceed.
3. **Choose a slice.** For multi-part work, outline the next one or few slices in plain language. Each slice should stand on its own, change a limited set of things, and have an observable completion condition. Start with a useful, low-dependency slice.
4. **Complete that slice.** Make the smallest coherent set of changes that achieves its stated outcome. Avoid bundling unrelated cleanup or speculative future work. Keep edits readable and easy to review.
5. **Show the result.** Summarize what changed, where, and how it meets the slice outcome. Mention checks performed only when relevant; do not claim checks that were not run. Call out remaining decisions or dependencies clearly.
6. **Continue at the right pace.** If the user requested approval or review between slices, stop at the checkpoint and wait. Otherwise, continue with the next slice when its direction is clear, while keeping changes incremental and reporting progress. Re-plan when feedback or discoveries change the scope.

## Slice sizing

- Prefer one independently understandable outcome per slice, such as one user-visible behavior, one document section, or one focused infrastructure change.
- A slice is too large when its result is hard to summarize, spans unrelated concerns, or cannot be reviewed without understanding several unfinished changes. Split it along meaningful boundaries.
- A slice is too small when it adds process overhead without making a useful outcome easier to inspect. Combine tightly coupled edits when they serve one outcome.
- For implementation work, keep each change set focused; for research or planning, deliver a concise finding or decision that can guide the next step.

## Communication

Before work that has meaningful scope, state the intended next outcome and any important assumption. Ask direct, bounded questions instead of making the user infer what decision is needed. Do not ask for confirmation on routine reversible choices or repeat questions the user has already answered.

At a review checkpoint, give the user enough context to assess the slice: its outcome, the key changes or artifact, any relevant verification, and the next decision if one is needed. Do not imply that the entire goal is complete while later slices remain.
