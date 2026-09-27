# Scoring guide: cumulative contribution points

Every distinct, accepted contribution earns points. There is **no maximum number of findings and no maximum score**. We keep a **bug subtotal** and an **improvement subtotal**, then add them for the total. There are no separate presentation or general-judgement points.

## The four tiers

| Accepted contribution | Points | Evidence required |
|---|---:|---|
| **Basic bug** | **2** | A real, reachable defect with bounded impact. Show the expected behaviour, the failure, the conditions and affected code, and how a fix could be checked. A reproducible local example or a complete static code trace is acceptable. |
| **Big bug** | **3** | A demonstrated material failure of a core workflow, evidence accuracy, consent/access boundary or important data integrity behaviour. Establish both reachability and consequence; a dramatic severity label is not enough. |
| **Decent improvement or feature request** | **10** | A useful, specific change grounded in this snapshot. Explain the problem, who benefits, the affected code/flow, a feasible approach, trade-offs and an acceptance check. |
| **Great improvement proposal or feature contribution** | **30** | A substantial improvement with clear value, detailed and credible implementation reasoning, measurable acceptance checks and considered dependencies, risks and trade-offs. It may be a well-supported proposal **or** a working, tested implementation. Its value and feasibility must be demonstrable from the submitted evidence. |

A bug is behaviour that contradicts a supported requirement or causes a demonstrable failure. A preference for a different design belongs under improvements. A bounded defect with a workaround will usually be a basic bug; a reachable failure that materially undermines the core workflow or an important boundary may be a big bug. Explain the actual impact rather than guessing a label.

A proposal does **not** need implementation to earn 30 points. Implementing a small change does not automatically make it a great improvement. Code volume, a PR title, a new framework or an attractive mock-up alone does not meet that threshold. “Improve the UI”, “add AI” or “make it scalable” without a concrete need, feasible design and checkable result does not qualify.

The assessor assigns the final tier and records the evidence and reason. Your suggested tier is a claim to support, not a self-awarded score.

## What counts as evidence

For a bug, establish:

- the requirement or expected behaviour;
- the relevant files/lines and reachable preconditions;
- a minimal reproduction **or** complete source-supported failure trace;
- expected versus observed or predicted behaviour, clearly labelled;
- practical impact, likely cause and a regression check.

A source-supported finding can earn the same points as a locally reproduced one. Where services are out of scope, a complete static argument is valid. Unsupported speculation is not a confirmed bug. Merely repeating the pack's disclosed limitations, such as unavailable live services or a simulated preview, earns no bug points. Demonstrating a separate defect in supplied code can still qualify.

For an improvement, explain the need in this version, what you would change and where, who benefits, why it is feasible, its risks/effort/trade-offs and how you would measure success. If you include code, provide the relevant patch or private PR/commit and say which checks actually ran. Do not describe unrun tests as passing.

## Count each underlying contribution once

1. **One root cause or underlying contribution earns one award within your submission.** Multiple symptoms, files, screens, commits or small tickets do not multiply it. Combine the evidence.
2. **Tiers do not stack.** A big bug earns 3, not 2 plus 3. A great improvement earns 30, not 10 plus 30.
3. **Bug and improvement points do not stack for the same work.** Merely fixing a reported bug is not a second improvement award. If the underlying contribution independently meets an improvement tier, that higher award replaces the bug award; it is not added to it. The assessor records the superseded entry and why.
4. **Distinct contributions in the same area can each count.** Explain the separate need or root cause and the separate value, rather than relabelling one fix.
5. **The same genuine issue found independently by different students can earn each student full credit.** There is no first-finder bonus. An issue being known internally does not disqualify it unless the supplied pack disclosed the relevant limitation to everyone.
6. Unsupported claims, duplicate entries and restated disclosed limitations earn zero. Report uncertainty honestly; there is no extra penalty for labelling a hypothesis as unverified.

## How the totals work

- **Bug subtotal** = 2 × accepted basic bugs + 3 × accepted big bugs.
- **Improvement subtotal** = 10 × accepted decent improvements + 30 × accepted great improvements.
- **Total** = bug subtotal + improvement subtotal.

All accepted, distinct entries count. The total is not a percentage and has no fixed maximum. Only the highest accepted award for each underlying contribution enters those subtotals. A missing assessment decision is not treated as zero: unresolved evidence, duplication or moderation is settled before an official total is finalised.

## Fair comparison

Everyone reviews the same frozen snapshot under the same rules. AI assistance and public documentation are allowed as described in the brief. Check the output and be able to explain your work. Paid tools, report length, polish, code volume, advanced Git techniques and commit count do not earn extra points. Using a private GitHub repository is a submission requirement, not a scoring bonus.

Prioritisation, clear evidence and honest limits help us understand your work but have no separate point allocation. A high score is one input to the placement decision, not proof of authorship or an automatic offer. We may ask you to explain a finding or demonstrate a small variation before making selections.
