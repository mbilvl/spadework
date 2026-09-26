---
title: Tech stack, CMS and hosting
output: 06-tech-stack.md
depends_on: [ia, scope]
tier: review
budget_usd: 1.50
research:
  max_searches: 6
  max_fetches: 8
  questions: |
    The project, as publicly described:
    {{ (public_context | trim) or "(No description was given. Research the general case of a small business website with a catalogue that non-developers maintain.)" }}

    Find current, citable facts for choosing how to build, edit and host a
    website like this one:

    1. Content management. Three to five options a non-developer can use to
       maintain pages and a structured catalogue or list of services, across
       different kinds (a hosted headless CMS, a traditional CMS on managed
       hosting, a hosted site builder). For each: plan names, prices, and
       the limits that matter (editor seats, number of records, languages or
       locales, bandwidth, image transformations). Quote the pricing page.
    2. Images. How each option, or a separate image service, resizes images
       and serves them in modern formats, and what that costs for a few
       thousand images.
    3. Hosting. Pricing for two or three hosts suited to a mostly static or
       cached site, and whether they serve from locations in or near the
       region named above.
    4. Data protection. Where each option stores data, and whether it offers
       a data processing agreement.

    Prefer the vendors' own pricing and documentation pages. Record each price
    exactly as shown, with its currency, billing period, and whether tax is
    included.
validate:
  max_lines: 220
  min_sources: 3
  required_headings:
    - Recommendation
    - Options
    - Image pipeline
    - Hosting
    - Total cost
    - Risks
    - Open questions
---
This document recommends how to build, edit and host the site, and what it
will cost to run. The developer quotes from it and the client approves the
running costs from it, so the recommendation must be unambiguous and every
cost checkable.

{% if brief.priorities %}The brief's priorities decide between options, most important first:

- {{ brief.priorities | join("\n- ") }}

{% endif %}Judge every option against, in this order: those priorities; the content
model in 04-information-architecture.md (every content type, field,
relationship and language must be supported); who will edit the site and how
often, from 01-scope.md; the running cost over three years; and how easily
the client could move away later.

Prices, plan limits and vendor facts come only from the research notes. If
the research did not find a price, write "price not found" in the table and
record it as a gap; never fill it in from memory. Treat what the research
could not answer the same way.

{% if brief.currencies %}Give every cost in {{ brief.currencies | join(" and ") }}. Where a source prices in another
currency, show the original figure; any conversion is a rounded
approximation and must say so.

{% endif %}Write these sections as `##` headings, in this order, with exactly these names:

- **Recommendation**: the recommended combination (CMS, how the site is
  built, hosting, image handling, forms) as a short table, then three to
  five reasons, each tied to a priority or to the content model.
- **Options**: a comparison table of the three or four realistic options,
  including the recommendation, against the criteria above, with one line
  on why each of the others lost.
- **Image pipeline**: how a photo gets from an editor's upload to a fast,
  correctly sized image on the page, and any plan limits the catalogue's
  size could hit.
- **Hosting**: where the site, the CMS and form submissions live; the
  region and data protection; backups; and what happens to the client's
  existing domain and email (ask if unknown).
- **Total cost**: one-off costs and recurring costs (monthly and yearly) per
  item, with the plan assumed and its source, and a three-year total. Keep
  build effort (a range of person-days) separate from software costs.
- **Risks**: only those that apply to this recommendation, such as lock-in,
  price changes, plan limits or a single maintainer.
- **Open questions**

Keep the document under 220 lines.
