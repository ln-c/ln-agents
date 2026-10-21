# The coordinator

Every sealed document and gate record is JSON, which the gates read and an approval binds to. A model, the **coordinator**, writes the readable version: a Markdown view of each document, a summary of every stop, and answers to questions. It is named in the project's `LN-AGENTS.md`:

```markdown
---
coordinator: gemini_3_8_flash_high   # a model that declares `coordination` in the registry
fallback: some_other_model           # optional
---
Your instructions on tone and layout, in plain Markdown.
```

- **What it writes**, under `.runs/<run>/views/`: `A1-proposal_a.md`, `A2-critique_a_by_b.md` and so on (every sealed document from the task specification to the review of the synthesis, plus the gate records), and `STOP-<n>-<node>.md` for each stop: blocked, paused, waiting for a signed decision, or finished. At a human decision the summary is a briefing on the candidates, with a recommendation labelled as advice. `run views <run>` writes them from a terminal; the A2A server writes the summary when a run stops and the documents in the background.
- **What it cannot do:** start, advance, sign, resume or decide anything. The commands in a summary come from a fixed table in code; the model's text is never run or parsed into an action. The JSON stays the record: every view says so, and says which model wrote it.
- **What checks it:** the same readiness, credential and data-class checks as a stage (a class that policy keeps off the cloud is refused before any call), and the subscription tool's sandbox. A reply that leaves out an identifier of its record is replaced by the raw record. With no coordinator, or when it fails, every file is still written, from code.
- **Where it is logged:** in its own hash chain, `views/provenance_<run>.jsonl`, not the run's, because the evidence a human signs includes the run's provenance tip and an advisory call must not void an approval already written. It charges no budget and seals nothing.
- **What it remembers:** each run has its own conversation, `views/conversation.jsonl`: after an answer, code appends the question as typed, the answer as shown, the model and the data class. The next answer for that run is given the last six turns (cut to a size) as notes, so asking about run A, switching to run B and coming back to A picks up where A was. The notes are labelled as data, not instructions, and every `<` in them is escaped; the files the answer is based on win over them. Each line carries the hash of the one before, so a file that was edited, reordered or cut is not used and not extended: the answer says it was not remembered. The next call is held to the strictest data class among the turns it is given, so something said under a class that policy keeps off the cloud never goes there through the memory. The file is a view: no gate reads it and the evidence a human signs does not include it. Code writes it, and it holds what you typed and the model's answers as shown (sanitized: links as text, HTML escaped); nothing in it is run. It is the memory of the coordinator's answers only; a model-written summary of a long conversation, and an `/authority <run>` command, are later changes.
- **Policy:** which models may coordinate is human-owned: the `coordination` capability in `policy/model_registry.yaml`. `LN-AGENTS.md` only chooses among them.
