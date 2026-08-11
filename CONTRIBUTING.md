# Contributing: Git and agent coordination

> ack3 Git policy v1 (2026-08-12). This file is intentionally identical across
> the independent repositories in the ack3 workspace. Update every copy in one
> coordinated change.

This workflow applies to humans, Codex, Claude Code, and every other automation
that writes to a repository. Repository-specific release instructions take
precedence when they are stricter.

## Non-negotiable rules

- Never do routine work directly on `main`, `master`, `stage`, or `release/**`.
  Changes enter a long-lived branch through a pull request.
- One task has one branch, one worktree, and one active writer. Two agents must
  never edit the same worktree or write to the same branch concurrently.
- Treat the remote branch and its pull request as the coordination record
  between machines. Do not hand off uncommitted or unpushed work.
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

Choose the base branch required by the repository's own instructions. Otherwise,
use `origin/HEAD`. Create a new branch and worktree from that remote ref:

```sh
git worktree add ../<repo>-<slug>-<owner> \
  -b <type>/<work-id>-<slug>-<owner> origin/<base>
cd ../<repo>-<slug>-<owner>
git push -u origin HEAD
```

Pushing the branch immediately makes the claim visible from the other machine.
Do not create an empty commit merely to reserve it.

- Claude Code: `claude --worktree <unique-name>` provides the required
  isolation. Confirm the generated branch follows this policy.
- Codex: managed worktrees may start on a detached `HEAD`. Run
  `git switch -c <branch-name>` and `git push -u origin HEAD` before the first
  commit.
- Manual sessions: never use `--force` to bypass Git's worktree safety checks.

## Branch names and ownership

Use:

```text
<type>/<work-id>-<short-slug>-<owner>
```

- `type`: `feat`, `fix`, `docs`, `content`, `perf`, `refactor`, `test`, `build`,
  `ci`, `chore`, or `hotfix`.
- `work-id`: the issue/ticket number; if none exists, use `YYYYMMDD`.
- `short-slug`: lowercase kebab-case description of the single outcome.
- `owner`: a stable person-machine-agent token, such as `jg-mbp-cdx1` or
  `jg-studio-cc2`.

Examples:

```text
feat/123-wake-router-jg-mbp-cdx1
docs/20260812-git-policy-jg-studio-cc2
```

Never reuse another active branch. The pull request body must name its current
active writer. A handoff changes that field; it does not permit two writers.

## Commits and pushes

Commit when a coherent checkpoint is complete, related checks pass, and the
diff can be explained in one sentence. Also commit and push before a handoff,
machine switch, context switch, or end of session.

- Keep every commit an isolated, complete change. Separate unrelated work.
- Use `type(scope): imperative summary` when a scope is useful, for example
  `fix(router): preserve versioned docs paths`.
- Stage explicit paths or patches. In a checkout that has any unrelated change,
  never use `git add -A`, `git add .`, or blanket formatting.
- Before committing, run `git diff --check`, inspect `git diff --staged`, and run
  the smallest relevant test or validation suite.
- Never commit secrets, local environment files, caches, logs, or generated
  output unless the repository explicitly tracks that output.
- Push every coherent commit. A commit that exists on only one machine is not a
  valid handoff or backup.

## Pull requests

All changes to long-lived branches require a pull request, including docs,
content, configuration, dependencies, CI, and agent instructions. The only
exception is an explicitly authorized emergency action; document it and follow
with a normal review.

Open a draft pull request after the first meaningful pushed commit when work is
still in progress. This makes ownership, scope, and status visible without
requesting a formal review. Keep one pull request focused on one outcome; split
it when reviewers would have to reason about unrelated changes.

Every pull request description records:

- the problem and intended outcome;
- the active writer and branch owner;
- the changed areas and deliberate exclusions;
- tests/checks run and their results;
- screenshots or generated artifacts when output is visual;
- risks, migrations, rollout or rollback notes;
- linked issues and cross-repository pull requests, including merge order; and
- a `Handoff` section with current `HEAD`, remaining work, and blockers.

Mark a draft ready only after self-reviewing the complete diff, removing
unrelated files, updating from the target branch, running required checks, and
resolving every known conversation. Obtain an independent review. Human approval
is mandatory for production/deployment behavior, security boundaries,
authentication, secrets, dependencies, CI workflows, data migrations, legal
text, and externally published claims.

Use squash merge by default so one pull request becomes one revertible change on
the long-lived branch. Preserve individual commits only when they are
independently useful. Delete the remote branch after merge.

## Handoffs and two-machine synchronization

Before handing work to another writer:

1. stop every process that can still edit the worktree;
2. commit only the task's files and push all commits;
3. verify `git status --short` is empty;
4. update the pull request's `Handoff` section with the exact `HEAD` SHA, checks
   run, remaining work, and the new active writer; and
5. tell the receiving writer that the branch is released.

The receiving writer fetches and verifies the remote SHA before editing. Never
copy a live worktree between machines and never let both machines continue the
same branch after a handoff. If work must proceed in parallel, split it into
separate branches and assign one integration owner.

Do not rewrite a published or handed-off branch. On a branch that has always had
one writer, a necessary rebase may be published with `git push --force-with-lease`
only after fetching and verifying that the remote did not advance. Never use
plain `--force`.

## Recovery and cleanup

- Do not use `git reset --hard`, `git clean -fd`, destructive checkout/restore,
  or branch/worktree deletion on changes whose ownership is uncertain.
- Do not use a stash as a cross-machine handoff. Never pop or drop a stash you
  did not create.
- If the base checkout is dirty, keep it untouched and branch from the clean
  remote ref in a new worktree.
- After merge, confirm the pull request is complete, remove only your clean
  worktree, run `git worktree prune`, and delete your local topic branch.

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
