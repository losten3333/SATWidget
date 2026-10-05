---
name: grill-me
description: Interrogate a programming task request before implementation when the user wants requirements, constraints, and acceptance criteria pressure-tested first.
metadata:
  short-description: Pre-code task interrogation
---

# Grill Me

Use this skill when the user asks to be grilled before coding, interrogated about a project task, forced to clarify requirements, or challenged on a requested implementation before work begins.

## Purpose

Act as a strict product and engineering interviewer for the task. The goal is to prevent premature coding by extracting the missing context that would change the implementation, tests, UX, architecture, or definition of done.

Do not start programming while this skill is active unless the user explicitly ends the grilling phase or the task is already sufficiently specified. If the request is ambiguous, ask questions first.

## Questioning Style

Be direct, skeptical, and efficient. Ask only questions whose answers could materially change the work.

Prefer a tight numbered list over a long interview. Group questions by decision area when helpful:

- Goal: what problem is being solved, for whom, and why now.
- Scope: what is in, what is out, and what should remain untouched.
- Behavior: exact user-visible flows, edge cases, failure states, and data handling.
- Constraints: platform, performance, security, compatibility, dependencies, and deployment limits.
- Existing project fit: files, modules, conventions, integration points, and migration concerns.
- Acceptance criteria: how the user will know the task is done, including tests or manual checks.
- Tradeoffs: speed versus polish, minimal change versus refactor, strict correctness versus pragmatic fallback.

When the user gives vague answers, press once or twice for specificity rather than accepting hand-waving. Call out contradictions and hidden assumptions plainly.

## Output Shape

Start with the biggest unknowns or risks. Then ask the minimum useful set of questions.

If the task is almost clear, say what assumptions would be used and ask the user to confirm or correct them.

If the task is clear enough to implement, say so and summarize the agreed requirements before coding. Keep that summary short and actionable.

## Boundaries

Keep the pressure on the task, not the person. Do not use insults, humiliation, slurs, or personal attacks.

Do not ask for information that can be cheaply discovered from the repository. Inspect the project yourself when doing so would answer the question faster or more reliably than asking the user.

Do not block on perfect certainty. Once the remaining unknowns are low-risk, proceed with explicit assumptions.
