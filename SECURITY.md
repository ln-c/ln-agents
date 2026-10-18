# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue: use **Security → Report a
vulnerability** on this repository (GitHub private vulnerability reporting). Include what you
found, how to reproduce it, and what an attacker could do with it. You should get a first
answer within a week.

Only the `main` branch is supported; there are no released versions yet.

## What counts

Anything that lets a model, a pull request or another local user get around what this
pipeline promises, for example:

- a model or a model's output that approves, decides, resumes, changes a budget or clears a
  veto, or impersonates the human (see `policy/approvers.yaml` and `src/agentic_pipeline/approvals.py`);
- escaping the build sandbox or writing outside an operation's allowed paths;
- sending data a data class forbids to a cloud model (`policy/data_classes.yaml`);
- the A2A server or `ln-hub` accepting a request from anything but loopback with the token;
- forging or replaying a signed approval, or breaking the provenance hash chain.

## Out of scope

- Problems that need an attacker who already runs code as your user with your unlocked SSH
  key: the approver key is the trust root (protect it with a passphrase or a hardware key).
- Model quality: a model writing a poor design is not a vulnerability unless a gate lets it
  through when it should not.
