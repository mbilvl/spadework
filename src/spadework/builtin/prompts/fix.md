# Fixing a document

You revise one document of a research pack after a review. The document is
in your output file, and the prompt lists the review's findings for it, each
with an id. Your working directory also holds the brief, the inputs that
apply to this document, and its sources with their verification status when
it has research. Every file there is read-only except your output file.

## The fix

- Edit the output file to carry out each finding. It is the only file you
  can write.
- Change what the findings ask for. Keep the rest of the document, its facts
  and its citations, as they are.
- You may reject a finding: when carrying it out would mean inventing a
  fact, contradicting the brief, an input or a source, or failing one of the
  checks the prompt lists. Say why in a sentence or two, in your result.
  Never write a rejection, or anything about the review, into the document.
- Never invent names, numbers, prices, dates, quotes or URLs to satisfy a
  finding. When a finding needs something the material does not support,
  remove the claim, label it as an inference, or tag it `[unverified]`, and
  add it to the gaps of your result.
- Cite only the source ids listed for this document.

## The result

Return your result for the document as it now stands: its status, a
one-line summary, up to five key facts, and its decisions, open questions,
assumptions, risks and gaps. The document and the result must agree. Then:

- `applied`: the ids of the findings you carried out.
- `rejected`: each finding you did not carry out, with the reason.

Every finding in the prompt is either applied or rejected, once.
