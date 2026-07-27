# Project Agent Instructions

## Git workspace policy

- Work only in the repository's existing checkout by default.
- Do not run `git worktree add` or create any additional Git worktree unless
  the project owner explicitly authorizes it for the current task.
- Git branches are allowed. Use one short-lived iteration branch at a time in
  the existing worktree.
- After an iteration is implemented and verified, push its branch and create a
  pull request.
- After a pull request is merged, update the primary branch and delete the
  merged iteration branch before starting the next one.
