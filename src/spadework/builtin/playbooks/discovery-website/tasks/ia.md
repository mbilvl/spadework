---
title: Information architecture
output: 04-information-architecture.md
depends_on: [scope, audience]
validate:
  max_lines: 250
  required_headings:
    - Sitemap
    - Page plans
    - Content model
    - Taxonomy
    - URL scheme
    - Open questions
---
This document defines which pages the site has, what goes on each, and how
the content is structured for editing. The developer builds from it, and
06-tech-stack.md, 07-content-plan.md and 08-seo.md all depend on it, so name
every page and content type once here; the rest of the pack reuses those
names.

Build it from what is in scope and the phasing in 01-scope.md, and from the
audiences and their needs in 02-audience.md. Every page must serve at least
one audience need. If you cannot say which, leave the page out.

Write these sections as `##` headings, in this order, with exactly these names:

- **Sitemap**: an indented list of every page at launch, main navigation
  first, with later-phase pages marked with their phase. Include the utility
  pages (contact, search if needed, not found) and the legal pages as a
  single line; 08-seo.md decides which legal pages apply.
- **Page plans**: one short block per page type (not per page): its purpose,
  its main audience, its sections from top to bottom, and its primary action.
- **Content model**: the content types editors will manage (for example a
  catalogue item, a category, a project or case study, a page, a news post),
  each as a table of fields: name, kind of field, required or optional, and
  translated or shared across languages. Show how types relate. This is the
  specification 06-tech-stack.md checks the CMS against, so be precise.
- **Taxonomy**: how items are grouped and filtered: categories, the
  attributes used as filters, and any tags. Use the client's own category
  names where the brief or inputs give them. Where they do not, show the
  structure with clearly marked placeholders and ask for the real list.
- **URL scheme**: the URL pattern for each content type, language prefixes
  if the site is multilingual, and rules for slugs. If the client has an
  existing site, say that its old URLs need redirects and ask for the list.
- **Open questions**

Size everything from the brief and inputs: how many items, how many
categories, how many languages. If a size is not given, ask for it rather
than assuming one, because it drives the CMS choice and the content effort.

Keep the document under 250 lines.
