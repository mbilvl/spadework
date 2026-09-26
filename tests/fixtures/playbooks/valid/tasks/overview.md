---
title: Overview
output: 01-overview.md
validate:
  required_headings: [Summary, Open questions]
---
Summarise {{ brief.title }} for {{ project.name }}.
{% include "partials/house.md" %}
{% if brief.priorities %}Priorities: {{ brief.priorities | join("; ") }}.{% endif %}
{% for file in files %}- {{ file.path }}: {{ file.heading }}
{% endfor %}
