---
title: Scope, assumptions and success criteria
output: 01-scope.md
validate:
  max_lines: 150
  required_headings:
    - Goals
    - In scope
    - Out of scope
    - Constraints
    - Assumptions
    - Success criteria
    - Phasing
    - Open questions
---
This is the first document in the pack, and every other document builds on
it. It says what the website is for, what it will and will not include, and
how the client will know it worked. The developer quotes from it; the client
should be able to read it and say "yes, that is what we asked for".

Work only from the brief and the inputs. This task has no web research. Where
an input is more recent than the brief and changes something, follow the
input and name it as the source.

{% if brief.priorities %}The brief's priorities, most important first. Every later document is
checked against them, so make sure the scope serves each one:

- {{ brief.priorities | join("\n- ") }}

{% endif %}Write these sections as `##` headings, in this order, with exactly these names:

- **Goals**: three to five business outcomes the site must deliver, in the
  client's own terms. Tie each one to a priority or to an input.
- **In scope**: what is built for launch, as a short table (item, notes).
  Be concrete: page types, the catalogue or service listings, forms,
  languages, integrations, migration of an existing site.
- **Out of scope**: what is excluded, including anything a reader might
  reasonably assume is included but is not.
- **Constraints**: budget, deadline, team, languages, and existing systems
  (domain, email, hosting, brand assets), only as the brief or inputs state
  them. Write "not stated" for any the brief is silent on.
- **Assumptions**: what this plan takes for granted that nobody has
  confirmed. Each one short enough for the client to confirm or reject.
- **Success criteria**: three to six criteria that can be checked after
  launch, each with how to check it. Where a baseline is needed and not
  known, say so.
- **Phasing**: the launch phase and any later phases, one line each on what
  goes in and why. No dates: 09-roadmap.md owns the calendar.
- **Open questions**: the client decisions that would change scope or cost.

{% if brief.extra.site_languages is defined and brief.extra.site_languages %}The brief sets the languages the website will be published in
(`site_languages`); state them under In scope.

{% else %}The brief does not say which languages the website will be published in.
Unless an input does, make it an open question.

{% endif %}Do not guess a budget, a deadline or a size (such as the number of products)
that the brief and inputs do not give. Record it as a gap and ask for it.
Every line should be about this client: cut any sentence that would be true
of any website project.

Keep the document under 150 lines.
