---
title: Plan
output: 03-plan.md
depends_on: [landscape, overview]
tier: review
budget_usd: 1.25
inputs: []
validate:
  max_lines: 80
  starts_with_h1: false
---
{% if research %}unexpected{% endif %}
{% for id, dep in deps.items() %}{{ id }} → {{ dep.output }}
{% endfor %}
{% if brief.currencies %}Give costs in {{ brief.currencies | join(" and ") }}.{% endif %}
