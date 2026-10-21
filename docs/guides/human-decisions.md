# Human decisions

A human state (`H1` design, `R1` merge, `R2` deploy) stops the run. The decision is a signed document, not a command-line flag:

1. `run evidence` (or `run approve`, or `/evidence` in the editor) writes a readable report to `.runs/<run>/approvals/<node>-evidence.md`. It lists every choice, each candidate's components, data flows, dependencies, vetoes and scores, and every finding with its judged probability and the synthesis's answer. It ends with the digests a signature binds to. The report is a view, rebuilt from sealed state each time, and model text in it is escaped so it cannot turn into links or HTML. `run approve` writes an approval document that binds the decision to that evidence, including the run's provenance tip, so an approval is single-use.
2. The human signs it with the approval key: `ssh-keygen -Y sign -f ~/.ssh/id_ed25519_ln_approval -n agentic-pipeline-approval <document>`. (`/decide` in the editor does 2 and 3 in a terminal window.)
3. `run decide` verifies the signature offline against `policy/approvers.yaml`, recomputes the evidence and takes the edge only if it still matches.

Only the keys listed in `policy/approvers.yaml` count. A model process has no way to produce an approval, but a key it could read and use could forge one. So the approver's key is a dedicated one (`~/.ssh/id_ed25519_ln_approval`): it is not the login key or the git-signing key, it has a passphrase, and it is never loaded into an ssh-agent. A hardware key (`ssh-keygen -t ed25519-sk`) is stronger still.
