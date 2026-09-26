---
title: Landscape of comparables
output: 01-landscape.md
budget_usd: 0.40
research:
  max_searches: 4
  max_fetches: 5
  questions: |
    The project, as publicly described:
    {{ (public_context | trim) or "(No description was given. Report the question below as unanswered.)" }}

    Find the five products, services or companies that compare most closely
    with the project above: direct competitors, or organisations that solved
    the same problem for a similar audience. For each one, record what it is,
    who it serves, how it presents itself and, if it says, how it prices its
    offer. Prefer its own pages, plus one independent source where there is
    one. If fewer than five genuinely compare, stop at the ones that do and
    say so.
validate:
  max_lines: 90
  min_sources: 3
  required_headings:
    - Comparables
    - What they show
    - Gaps
---
A quick first look at what already exists around this project: five
comparable products, services or companies, and what they show. The reader
uses it to sharpen the brief and to prepare the next conversation with the
client; 02-questions.md builds on it.

Use only the comparables in the research notes, and do not add any from
memory. A weak comparable is worse than a missing one: if the research found
fewer than five that genuinely compare, show those and say how many are
missing.

Write these sections as `##` headings, in this order, with exactly these names:

- **Comparables**: a table with one row per comparable: what it is, who it
  serves, how it presents or prices itself (prices in the currency the
  source uses), and the single most relevant thing about it for this project.
- **What they show**: three to five lessons that hold across the
  comparables, each tied to this project as the brief describes it: what to
  copy, what to avoid, or where there is room to be different.
- **Gaps**: what the research could not establish, and what would settle it.

{% if brief.priorities %}Weigh the lessons against the brief's priorities, and say which priority
each one bears on.

{% endif %}Keep the document under 90 lines.
