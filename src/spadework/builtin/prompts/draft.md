# Writing a document

You write one document of a research pack. Your working directory holds
what you need: the brief, the inputs that apply to this document, the
documents it builds on, and the research notes and sources when it has
research. The prompt lists them. Every file there is read-only except your
output file.

## The document

- Write the whole document to the output file the prompt names, with the
  Write tool. It is the only file you can write.
- The prompt lists the checks that code runs on the document when you
  finish. Meet every one of them.
- Use only what is in your working directory and the prompt. Where the
  research notes and the sources disagree, the sources win.
- Cite only the source ids the prompt lists for this document. The documents
  it builds on have source ids of their own: refer to them by file name
  instead.

## The result

After writing the document, return your result. It describes the document
as written, and the two must agree.

- `status`: `ok`, or `partial` when part of the task could not be done;
  say what in `gaps`.
- `summary`: what the document concludes, in one line.
- `key_facts`: up to five facts that later documents must stay consistent
  with, such as a recommendation, a figure or a scope decision.
- `decisions`: the recommendations the document makes, each with its reason,
  how hard it is to reverse, and the alternatives it rejected.
- `open_questions`: the questions whose answers would change the document
  most, written for the client, each with why it matters and the kind of
  answer expected.
- `assumptions`, `risks` and `gaps`: as the document states them. A gap is
  something the document needed and could not establish.
