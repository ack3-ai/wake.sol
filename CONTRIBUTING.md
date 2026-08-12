# Contributing: Git and agent coordination

> ack3 Git policy v2 (2026-08-12). This file is intentionally identical across
> the independent repositories in the ack3 workspace. Update every copy in one
> coordinated change.

This workflow applies to humans, Codex, Claude Code, and every other automation
that writes to a repository. Repository-specific release instructions take
precedence when they are stricter.

## Non-negotiable rules

- Never do routine work directly on `main`, `master`, `stage`, or `release/**`.
  Changes enter a long-lived branch through a pull request.
- One task has one stable branch. Use a dedicated worktree; never let two agents
  edit the same worktree.
- Do not intentionally run simultaneous writers on one branch. Git's
  non-fast-forward push rejection is the safety net if two machines overlap;
  never bypass it with a force push.
- The remote task branch is the cross-machine synchronization record. Pull
  requests are for review and merging, not routine agent coordination.
- If a checkout contains changes you did not create, do not stage, stash,
  discard, or reformat them. Leave it in place and create a clean worktree.
- Each repository gets its own branch and pull request. A change spanning
  repositories uses the same work ID and slug, with the pull requests linked.

## Start every task from a clean worktree

First inspect the repository; do not assume its base branch is named `main`:

```sh
git fetch origin --prune
git status --short --branch
git symbolic-ref --short refs/remotes/origin/HEAD
git worktree list
```

Before creating a branch, check whether the task already has a remote branch.
If it does, resume it as described below. Otherwise, choose the base branch
required by the repository's own instructions, or use `origin/HEAD`, and create:

```sh
git worktree add ../<repo>-<slug> \
  -b <type>/<work-id>-<slug> origin/<base>
cd ../<repo>-<slug>
git push -u origin HEAD
```

Pushing the branch immediately makes the task visible from the other machine.
Do not create an empty commit merely to reserve it.

- Claude Code: `claude --worktree <unique-local-name>` provides isolation.
  Confirm the generated branch follows this policy.
- Codex: managed worktrees may start on a detached `HEAD`. Create or switch to
  the stable task branch and set its upstream before the first commit.
- Manual sessions: never use `--force` to bypass Git's worktree safety checks.

## Branch names

Use:

```text
<type>/<work-id>-<short-slug>
```

- `type`: `feat`, `fix`, `docs`, `content`, `perf`, `refactor`, `test`, `build`,
  `ci`, `chore`, or `hotfix`.
- `work-id`: the issue or ticket number; if none exists, use `YYYYMMDD`.
- `short-slug`: a lowercase kebab-case description of the single outcome.

Examples:

```text
feat/123-wake-router
docs/20260812-git-policy
```

Do not include a person, machine, tool, model, agent, or session identity in a
branch name. Local worktree directory names may include a local-only suffix when
needed to avoid a collision.

## Continuous synchronization and commits

Every agent turn that modifies files is a potential machine switch. Before
yielding control, the agent must:

1. stage only files owned by the task;
2. run the relevant checks;
3. commit the coherent current state;
4. push it to the task branch; and
5. report any work that could not be committed or pushed.

Incomplete but coherent work may use a `wip: checkpoint ...` commit. Never
include known-broken, secret, generated, or unrelated files merely to create a
checkpoint. Structure work so a mutating turn normally ends at a coherent
checkpoint.

This policy authorizes agents to create and push checkpoint commits to the
current task branch without asking the user after each one. It does not
authorize direct pushes to long-lived branches, force pushes, merges, or the
inclusion of unrelated files.

- Keep every commit an isolated, explainable change. Separate unrelated work.
- Use `type(scope): imperative summary` when a scope is useful, for example
  `fix(router): preserve versioned docs paths`.
- Stage explicit paths or patches. In a checkout with unrelated changes, never
  use `git add -A`, `git add .`, or blanket formatting.
- Before committing, run `git diff --check`, inspect `git diff --staged`, and run
  the smallest relevant test or validation suite.
- Never commit secrets, local environment files, caches, logs, or generated
  output unless the repository explicitly tracks that output.

## Resuming on another machine

At the start of work:

1. fetch from `origin`;
2. identify the existing remote task branch;
3. create or reuse a clean worktree tracking that branch;
4. verify local `HEAD` matches the remote branch; and
5. continue from the latest commit and diff.

The remote task branch is the synchronization source of truth. Never use a
stash, copied worktree, pull-request prose, or machine-local metadata as the
handoff mechanism.

Never force-push a task branch. If a push is rejected because the remote
advanced, stop editing, fetch, inspect the competing commits, and reconcile
deliberately. Do not overwrite remote history. If the competing changes cannot
be reconciled safely, request a human decision with the exact commits and
conflict; routine checkpoints require no human coordination.

## Pull requests

All changes to long-lived branches require a pull request, including docs,
content, configuration, dependencies, CI, and agent instructions. The only
exception is an explicitly authorized emergency action; document it and follow
with normal review.

Open a pull request when the change is ready for review. Do not open a draft
merely to claim a branch or publish agent status. Keep one pull request focused
on one outcome; split unrelated work.

The description should contain only what a reviewer needs:

- the problem and intended outcome;
- changed areas and deliberate exclusions;
- tests or checks run and their results;
- screenshots or generated artifacts when output is visual;
- material risks, migrations, rollout, or rollback notes; and
- linked issues and cross-repository pull requests, including merge order.

Request human attention once the complete diff is ready. Do not use PR comments
as a heartbeat or append a handoff log after every checkpoint; pushed commits
already update the pull request. Human approval remains mandatory for
production or deployment behavior, security boundaries, authentication,
secrets, dependencies, CI workflows, data migrations, legal text, and
externally published claims.

Use squash merge by default so checkpoint commits remain useful during
development without cluttering the long-lived branch. Preserve individual
commits only when they are independently useful. Delete the remote task branch
after merge.

## Recovery and cleanup

- Do not use `git reset --hard`, `git clean -fd`, destructive checkout or
  restore, or branch/worktree deletion on changes whose ownership is uncertain.
- Do not use a stash as cross-machine synchronization. Never pop or drop a
  stash you did not create.
- If the base checkout is dirty, keep it untouched and branch from the clean
  remote ref in a new worktree.
- After merge, confirm the pull request is complete, remove only your clean
  worktree, run `git worktree prune`, and delete your local task branch.

## Repository enforcement for maintainers

Guidance is not a lock. Configure GitHub rulesets for every long-lived branch to
require pull requests, passing status checks, resolved conversations, and the
appropriate approvals; block force pushes and deletions; and prevent bypasses.
Use `CODEOWNERS` for sensitive paths, enable automatic head-branch deletion, and
use a merge queue when concurrent pull requests frequently invalidate checks.
Local hooks are useful feedback but are not enforcement; CI and server-side
rules are authoritative.

## Basis

This policy follows the official guidance for
[GitHub flow](https://docs.github.com/en/get-started/using-github/github-flow),
[reviewable pull requests](https://docs.github.com/en/pull-requests/concepts/helping-others-review-your-changes),
[protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches),
[Git worktrees](https://git-scm.com/docs/git-worktree),
[Codex worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees),
and [Claude Code worktrees](https://code.claude.com/docs/en/worktrees).
