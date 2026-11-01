# 0015 — Interactive session

Status: accepted (2026-10-31)

Context: the specification's CLI section starts with `dfm` and `dfm chat`, but no build issue
covered them, so `dfm` printed "the interactive session is not built yet". Every other command
runs one task and exits: it loads the local model (tens of seconds for a 27B model), shows
nothing until the final report, and forgets the task afterwards.

Decision (`dfm/interactive/`):

- **One Runtime per session.** The model pool, policy, session approvals and trace live as long
  as the session. A model loads on first use and stays loaded until `idle_unload_s` (10 minutes)
  without requests. The trace header has `mode: interactive`; each request adds a
  `user_request` event to the same trace.
- **Routing per request.** The router's decision picks the turn type. Code work with a
  deterministic or Apple FM route runs `AgentLoop`, as `dfm implement` does. Review requests
  review the `HEAD` working-tree diff. Repository explanations, resume questions and requests
  no rule matched go to `ask`, which never edits. `/do` and `/ask` override the router.
- **Live display.** `Runtime.listeners` receive every `rt.event` after it is traced. The
  display prints the model's chosen action with its target and reason, which are new optional
  fields on `model_finished` (protocol v1: optional fields are compatible). It also prints
  reads, a unified diff of each edit (secret files excluded), commands, test counts and policy
  refusals. Model-controlled text is printed literally. A listener exception is logged and never
  stops the work.
- **Per-request checkpoints.** Each agent turn uses checkpoint id `<session>-t<n>`, so the
  pre-edit content of a file is recorded per request. `/undo` (`Checkpointer.undo`) restores
  the latest request's files, deletes files it created, and skips any file whose current hash
  differs from what DFM left. Those are the developer's changes now.
- **Git guard hand-off.** Files that an earlier turn edited, and that still hash to what DFM
  left, are passed to the next turn's `GitGuard` as planned paths, so DFM can keep working on its
  own changes. Without this, turn two would refuse them as "uncommitted changes". A file the
  developer edited since is no longer DFM's and stays protected.
- **Conversation context.** The last six finished requests (request, status, summary, changed
  files) are given to the coder as one history entry marked "for context only". Questions get
  no history yet. `/clear` stops passing earlier requests; it does not touch files.
- **Interruption.** Each request runs as a task. SIGINT cancels it, and process cancellation
  kills its process group. The turn is recorded as `interrupted` with its edits kept, and
  `/undo` reverts them. Approvals are read through prompt_toolkit (`InteractiveApprover(ainput=…)`),
  so Ctrl-C at a confirmation is a denial rather than a thread blocked on `input()`.
- **Input.** prompt_toolkit (new runtime dependency) provides line editing, a history file
  (`<data>/prompt_history`, mode 0600) and slash-command completion. Rich draws the output.

Not in this slice: streaming model text while it thinks, conversation history for questions,
switching models within a session, and `dfm commit`.

Consequences: requests in one session share one session approval set, as within one
`dfm implement` run; `dfm resume` starts with none. The history file holds what the developer
typed. Without a terminal, `dfm` lists the commands; `dfm chat` is the explicit form.

Addendum (2026-10-31, first use of 0.0.3): the session started in the home folder, treated
every file under it as the repository, and answered `ls` with RepoCanon's package list, because
questions went to `ask`, which sees only the repository summary. Changes:

- **Start folder.** A session refuses the home folder, its parents and the filesystem root, and
  warns outside a Git repository.
- **Questions look at the code.** With a coder, a question runs `AgentLoop.answer` (read-only:
  read, search, read-only tests, and `run` only for commands classified `SAFE_READ`; edit
  actions are refused). It uses its own instructions and tool list, and the answer is the
  `done` summary. Without a coder, questions still go to `ask`.
- **`!command`.** The developer's own command runs through `CommandRunner` with
  `proposed_by="user"` and the sandbox. Its output is shown and is never sent to a model.
- **Model name.** The banner shows `org/model` from the Hugging Face cache path, not the
  snapshot hash.

Addendum (2026-10-31): interruption now reaches the model. Cancelling the request task cancels
the in-flight `generate`, which sends the worker a `cancel` message; the worker stops after its
current token and stays loaded (ADR 0013). Only `CancelledError`/`KeyboardInterrupt` record a
turn as `interrupted`; any other exception fails the turn, is shown, and the session continues.
Services the task started are released by the agent loop however the task ends.
