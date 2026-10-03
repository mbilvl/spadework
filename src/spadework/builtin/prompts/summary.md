# Writing the summary

You write `00-summary.md`, the first document of a research pack: the page a
busy reader reads instead of the rest. Your working directory holds the
brief, the inputs, every document of the pack, and `records/digest.md`,
which lists what each document's session reported. The prompt lists every
document with its state, and every open question in the pack with its id.
Every file there is read-only except your output file.

## The document

Write these parts, in this order, and nothing else:

1. A level-1 heading with the pack's title, then one line that says what the
   pack is for.
2. `## Executive summary`: at most 12 bullets, the most important first. Each
   is a finding, a recommendation or a risk the reader must know, with the
   document it comes from, as "(see 06-tech-stack.md)".
3. `## Key decisions`: the decisions the pack recommends, each with its
   reason and the document that makes it. A table works well.
4. `## Open questions`: at most 10 questions for the client, numbered, the
   most blocking first. Merge questions that ask the same thing in different
   documents. Each says why it matters.

Code appends more sections after yours: the documents with their state and
checks, the source verification counts, review findings a person must act
on, and how the pack was produced. Do not write those.

- Say plainly when a document the summary rests on is failed, invalid,
  edited by a person (pinned) or missing.
- Take facts only from the pack's documents and records, the brief and the
  inputs. Cite no `[S#]` ids: attribute each fact to its document by file
  name.

## The result

- `status`: `ok`, or `partial` when part of the pack could not be summarised.
- `open_questions`: the questions of your Open questions section, in the same
  order, each on one line, with why it matters and the ids of the questions
  it merges: at least one, each once, such as `stack:Q1`, taken from the
  list in the prompt.
