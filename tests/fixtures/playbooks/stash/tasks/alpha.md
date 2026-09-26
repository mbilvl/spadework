---
title: Alpha
output: 01-alpha.md
---
{% import "partials/box.md" as p %}{% if p.box.append(brief.body) %}{% endif %}{% set stash = p.ns %}{% set stash.value = brief.title %}Alpha body
