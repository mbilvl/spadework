---
title: Landscape
output: 02-landscape.md
depends_on: [overview]
inputs: ["calls/**", "*.txt"]
research:
  max_searches: 4
  max_fetches: 5
  questions: |
    For {{ public_context }}:
    - five comparable companies for "{{ task.title }}", in {{ brief.language }}
validate:
  min_sources: 2
---
Build on {{ deps.overview.output }}:
{% for fact in deps.overview.key_facts %}- {{ fact }}
{% endfor %}
{{ research.notes }}
{% for source in research.sources %}[{{ source.id }}] {{ source.title }}
{% endfor %}
