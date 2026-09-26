---
title: Design direction
output: 05-design-direction.md
depends_on: [audience, references]
validate:
  max_lines: 150
  required_headings:
    - Art direction
    - Palette
    - Typography
    - Photography
    - Components
    - Open questions
---
This document gives a designer enough direction to start mock-ups, and gives
the client something concrete to react to. It is a direction, not a finished
design system.

Ground each choice in 02-audience.md (who must trust the site, and what
persuades them) and in 03-references.md (the patterns to adopt and to avoid).
When a choice follows a reference, name the pattern. Treat the brand assets
the brief or inputs describe (logo, colours, typefaces) as fixed, and do not
propose a new brand unless the brief asks for one.

Write these sections as `##` headings, in this order, with exactly these names:

- **Art direction**: a short paragraph on the overall feel, then three to
  five principles, each in one line with the reason it suits these
  audiences.
- **Palette**: a table of four to seven colours: role, hex value, and where
  it is used. Build on the client's colours if they are known; otherwise
  propose a starting palette and mark it as a proposal. Name the pairs
  meant for body text; they must meet WCAG 2.2 AA contrast (4.5:1). Give a
  contrast ratio only if you have calculated it; otherwise mark the check
  as still to do.
- **Typography**: one or two typefaces, their licence (prefer ones that are
  free to use on the web), a type scale, and whether they cover the
  characters of every site language.
- **Photography**: the style (subject, light, framing, backgrounds), what to
  avoid, and how to handle the existing photos the brief or inputs describe.
  07-content-plan.md turns this into a shot list, so give rules, not a list.
- **Components**: the reusable interface pieces the audiences' needs call for
  (for example an item card, a specification table, a filter bar, a quote
  request form, a strip of certifications), one line each, with those needed
  at launch marked.
- **Open questions**

Every choice needs a reason that belongs to this client. Drop any principle,
colour or component that would suit any company in any industry.

Keep the document under 150 lines.
