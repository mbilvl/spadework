# Example project: Acme Fabrication

A complete spadework project you can inspect and run. **Everything in it is
fictional.** Acme Fabrication, its staff, its figures and the client call do
not exist, and any resemblance to a real company is accidental.

It uses the built-in `discovery-website` playbook to scope a catalogue website
for a small stainless-steel fabricator in northern Portugal.

| File | What it is |
|---|---|
| `spadework.toml` | The project config: playbook, models, budget and limits |
| `brief.md` | The brief: front matter that spadework reads, then free notes |
| `inputs/2026-09-30-client-call.md` | Notes from a later client call. Some of it changes the brief, for example Spanish at launch. |

## Try it

Validate the project and see the plan. This spends nothing:

```bash
spadework check -C examples/acme-fabrication
```

See exactly what the web-facing research sessions would see, which is only
the brief's `public_context`:

```bash
spadework check -C examples/acme-fabrication --show-public
```

Running it calls Claude and **costs money**, up to the $6.00 budget in
`spadework.toml`. Copy the folder outside the repository first, so the pack
(`out/`) and the engine state (`.spadework/`) stay out of it:

```bash
cp -R examples/acme-fabrication ../acme-demo
spadework run -C ../acme-demo
```

Then change something, for example add `es` to `site_languages` in the copy's
`brief.md`, and run `spadework check -C ../acme-demo` again to see which tasks
become stale and why.
