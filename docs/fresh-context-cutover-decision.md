# Owner decision: preserve topic identity, start fresh Codex context

On 2026-10-08 the Owner clarified the production migration scope: each retained
Telegram topic must keep its name and bound canonical working directory, but its
pre-cutover Codex chat context does not have to continue. The separate staging
forum and its test topics are disposable. This decision narrows the V4 requirement
to import legacy session provenance; it does not relax the other release gates.

For every retained production topic, the cutover inventory comparison must still
prove the exact AgentInstance/chat/thread identity, name, canonical cwd, routing,
and engine/exec mode. The `inventory compare --binding-plan` check additionally
proves the reviewed topic-scoped account/model/reasoning values match both CUS
desired and Runtime effective state; live process evidence still proves Applied.
Topics already
deleted from Telegram are excluded only through the existing digest-bound Owner
decision process. Two topics with the same cwd remain separate bindings.

The new Runtime must start a fresh Codex session for an existing managed topic.
Do not feed pre-cutover tmux state into the new AgentInstance or accidentally
reattach a legacy pane. Preserve the old state and session files in the rollback
artifact/control period; do not delete or overwrite them as a shortcut. If a
legacy session appears in the upstream `/resume` picker, its unknown binding
provenance must be denied before Codex starts. Sessions created after cutover
still require same-binding and same-account provenance for `/resume`.

Qualification must exercise a fresh session for a retained topic while proving
its name, cwd and profile are unchanged, plus legacy-resume denial. Native new
sessions still require the full Codex storage lifecycle and a staging rollback
drill. Rollback must restore the old working service/configuration and preserve
newly created session files; it need not provide seamless continuation of the
pre-cutover conversation in the new architecture.

This decision is not approval to switch production before the independent
upstream first-prompt/Telegram E2E and retirement-race gates pass. It is also not
approval to delete the legacy repository, overlay, or production chat/session
files. Cleanup remains a separate Owner decision after the control period.
