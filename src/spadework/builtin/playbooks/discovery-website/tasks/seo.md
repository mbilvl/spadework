---
title: Search, local presence and legal pages
output: 08-seo.md
depends_on: [ia]
research:
  max_searches: 5
  max_fetches: 6
  questions: |
    The project, as publicly described:
    {{ (public_context | trim) or "(No description was given. Report each question below as unanswered.)" }}

    1. Search terms. Find evidence of the words the customers described above
       use when they look for this kind of product or service, in each
       language the site will use: trade directories, industry associations,
       and the page titles and headings of businesses in the same trade.
       Record search volumes only where a source states them.
    2. Structured data. Which schema.org types and properties fit this kind of
       business and what it offers, and what the major search engines' own
       documentation says they use.
    3. Local presence. Which map listings, business directories and trade or
       industry directories matter for this trade in this country or region.
    4. Legal pages. Which pages and notices a business website in this
       jurisdiction must or should carry (for example a privacy notice,
       cookie consent, company details or a legal notice, an accessibility
       statement, consumer complaints information). Prefer official and
       regulator sources.
validate:
  max_lines: 180
  min_sources: 3
  required_headings:
    - Keywords
    - Structured data
    - Technical basics
    - Local presence
    - Legal pages
    - Open questions
---
This document says how the site gets found, and what it must carry to be
compliant. The developer implements it; the client acts on the local presence
and has the legal texts written.

Map everything onto the pages, content types and URL scheme in
04-information-architecture.md, using its names. Facts about search terms,
directories and legal requirements come only from the research notes; what
the research could not answer is a gap, not a guess.

Write these sections as `##` headings, in this order, with exactly these names:

- **Keywords**: a table of keyword themes by page type and site language,
  using the terms the research found in use. No search volumes or difficulty
  scores unless a source gives them. Mark themes that are your inference, and
  suggest the client checks them with a keyword tool before launch.
- **Structured data**: which schema.org types go on which page types, with
  their key properties and the content-model fields that fill them.
- **Technical basics**: only what this site needs, for example per-language
  URLs and links between language versions, the XML sitemap, redirects from
  an existing site, image and page-speed points for a photo-heavy site, and
  the metadata fields editors must fill in.
- **Local presence**: the listings and directories the research says matter,
  and what the client must do for each.
- **Legal pages**: a table of the pages and notices that the research says
  apply in this jurisdiction: what each must contain, and its source. Say in
  one line that this is a checklist for the client to confirm with their
  legal adviser, not legal advice.
- **Open questions**

Keep the document under 180 lines.
