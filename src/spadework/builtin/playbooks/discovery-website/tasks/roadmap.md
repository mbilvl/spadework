---
title: Roadmap, decisions and next steps
output: 09-roadmap.md
depends_on: [scope, audience, references, ia, design, stack, content, seo]
tier: review
budget_usd: 1.25
validate:
  max_lines: 200
  required_headings:
    - Phases
    - Calendar
    - Decision log
    - Risks
    - Next steps
---
This is the last document in the pack and the one the client is most likely
to read in full. It turns the other eight into a plan: what happens in what
order, what has been decided, what could go wrong, and what to do next.

Read all eight documents and treat their key facts as the settled position.
Collect decisions rather than re-arguing them. Where two documents disagree
(for example on languages, the page list, effort or cost), do not pick one
silently: record the conflict under Risks with both filenames, and put the
decision needed at the top of Next steps.

Write these sections as `##` headings, in this order, with exactly these names:

- **Phases**: a table with one row per phase from 01-scope.md: what it
  contains, which of the brief's priorities it delivers, what it depends on,
  and its effort as a range of person-days. Take effort from the other
  documents where they give it; otherwise estimate it and label it as an
  estimate.
- **Calendar**: a week-by-week plan for the launch phase. Show the client's
  tasks (content, photos, approvals, answers to open questions) alongside the
  build, because they are usually the critical path. Use weeks counted from
  kick-off unless the brief or inputs give real dates.
- **Decision log**: a table of the decisions made across the pack: the
  decision, the reason, the document it comes from, and how reversible it
  is.
- **Risks**: the top five to eight risks for the project as a whole, not
  repeated document by document: likelihood, impact, mitigation, and who
  owns it (client or developer).
- **Next steps**: numbered, most urgent first, each with who does it. The
  first steps unblock the calendar: the answers, assets and approvals it is
  waiting on.

{% if brief.extra.launch_target is defined and brief.extra.launch_target %}The brief gives a launch target (`launch_target`). Plan the calendar back
from it, and if the plan does not fit, say so plainly under Risks.

{% endif %}{% if brief.currencies %}Give money figures in {{ brief.currencies | join(" and ") }}, consistent with 06-tech-stack.md.

{% endif %}Keep the document under 200 lines.
