# Start with an issue number

Give the assistant the repository and `Implement issue #N following CONTRIBUTING.md`. The issue is the task specification. GitHub credentials and Gemini keys stay in the developer's local environment.

Run these commands once in each local clone (Git 2.37 or newer). The first `git push` publishes the task branch to origin and sets its upstream.

```sh
git config --local push.autoSetupRemote true
git config --local push.default simple
```

1. Read [AGENTS.md](AGENTS.md), [RULES.md](RULES.md), [.agents/rules](.agents/rules), [architecture](docs/architecture.md), and [data contracts](docs/data-contracts.md). Read the complete issue with `gh issue view N --comments`. Check its responsible human, allowed paths, acceptance criteria, actual dependencies and branch name. Ask the owner about conflicting scope.
2. Inspect the worktree and latest main. Run `git fetch origin` and check that dependency PRs reached `origin/main`. Mark blocked work with a reason. Start a task branch when its prerequisites are satisfied.
3. From a clean clone run `git switch --no-track -c BRANCH origin/main`. To resume, switch to the existing task branch. For a separate writer use `git worktree add --no-track -b BRANCH ../work-ISSUE origin/main`. Substitute the actual issue branch. Branch prefixes are feat, fix, test or docs followed by `/issue-N-short-description`.
4. Set the issue to In Progress. One human owns it and chooses one primary assistant. Implement only the requested behavior and owner tests. A team can inspect synthetic fixtures before upstream integration is complete when the issue explicitly supports that path.
5. Run focused tests, `python -m pip check` and `python -m pytest -q`. Review `git status --short`, `git diff origin/main...HEAD`, pending changes, untracked files and the staged diff. Record real results and unresolved limitations.
6. Stage the intended files, commit with an English title and run `git push` from the task branch. Open a PR against main. Include `Closes #N`, test evidence and the relevant acceptance checkboxes. Set In Review and request another human's review.
7. Wait for CI and review. Reviewers check evidence, failure states and scope. Resolve feedback before merging. A second human approves; the author cannot substitute their own review. Leave the task open if an acceptance criterion is pending.

## Triage and repository settings

Ready means its implementation can start. Blocked identifies unfinished prerequisites or an external blocker. Use In Progress and In Review for active work. Testing also serves as the task-type label for tests and benchmarks; combine it with Ready, Blocked or the current work state. Merged and accepted issues are closed. The milestone contains the actual delivery backlog.

Use personal labels for ownership and Assignees for accepted collaborators. AI labels designate recommended assistants. Use the [ready queue](https://github.com/meiirzhan04/ai-requirements-validator/issues?q=is%3Aopen%20label%3AReady), [blocked queue](https://github.com/meiirzhan04/ai-requirements-validator/issues?q=is%3Aopen%20label%3ABlocked) and [milestones](https://github.com/meiirzhan04/ai-requirements-validator/milestones) to coordinate across owners. Clear the dependency blocker after checking upstream merges.

Require one independent approval and a green **Offline tests** check for feature PRs. This team rule also applies when server-side protection is unavailable. A failed or skipped live probe must be reported separately from CI.
