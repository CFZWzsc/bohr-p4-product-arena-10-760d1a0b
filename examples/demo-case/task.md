# Demo task

Build a small evidence-bound knowledge graph from the two synthetic papers in
`material.md`:

1. extract entities (researchers, materials, methods, tasks, metrics, conclusions)
   that actually appear in the text;
2. extract only directly supported relations, with type, direction and conditions;
3. keep a separate `source_id` per paper and bind every node/edge to a paragraph
   id and a verbatim quote (JATS-style paragraph ids, not PDF page numbers);
4. record alias merge decisions, including pairs that must **not** be merged;
5. run at least three real queries and report their result subgraph, supporting
   evidence and any relation that cannot be determined.

Anything not stated in the material must be reported as an uncertainty. This is
a synthetic example, not official case output.
