---
name: commit-standard
description: Commit message standard (based on Conventional Commits). Always use this whenever the user asks for help writing, reviewing, or fixing a commit message, or asks how to name/structure a commit.
---

# Commit Standard

Based on [Conventional Commits](https://www.conventionalcommits.org/).

## Format

```
<type>(<optional scope>): <short description>

<optional body>

<optional footer>
```

## Types

| Type | Use |
|---|---|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation-only changes |
| `style` | Formatting, whitespace, semicolons (no logic change) |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `perf` | Performance improvement |
| `test` | Adding or fixing tests |
| `chore` | Maintenance tasks (deps, configs, build) |
| `ci` | Continuous integration changes |
| `build` | Changes to the build system or external dependencies |
| `revert` | Revert a previous commit |

## Scope

Optional, in parentheses, indicates which part of the project is affected:

```
feat(auth): add Google login
fix(deploy): fix timeout in SSH connection
```

## Description

- Lowercase, no trailing period
- Imperative mood: "add", not "added" or "adds"
- Short (ideally < 72 characters)

## Breaking changes

Marked with `!` after the type/scope, or with a `BREAKING CHANGE:` footer:

```
feat(api)!: change response format of /status

BREAKING CHANGE: the `state` field is now an enum instead of a string.
```

## Examples

```
feat(instance-manager): support containerless deployment
fix(cli): fix argument parsing with spaces
docs(readme): update installation instructions
refactor(core): extract SSH connection logic into its own module
chore(deps): update minor dependencies
```

## Release-alignment commits (`chore(release)`)

In monorepos where sibling projects share a version number (see the
`version-standard` skill), a `chore(release): align <a> and <b> at <version>`
commit exists only to bump the version — it must carry no logic changes.

- Never fold the actual feature/fix/refactor work into the release-alignment
  commit. Land it first as its own properly typed commit(s) (`feat`, `fix`,
  `refactor`, ...), then follow with the `chore(release)` commit containing
  only version bumps (and, if unavoidable, generated artifacts like compiled
  locale files).
- A large, multi-part change (e.g. "add X, fix Y, complete Z") is a signal to
  split into one commit per logical piece before aligning versions — not a
  reason to bundle everything into one oversized commit. Large single commits
  are harder to review, bisect, and roll back in isolation.
- If asked to write a `chore(release)` commit message for a diff that
  clearly contains non-trivial logic changes, point this out and suggest
  splitting before writing the message.
- When the user asks to commit work *and* raise the version in one
  request, that means the full flow above: one commit per logical piece
  first, then the release-alignment commit. Never land everything as a
  single `chore(release)` commit — bundling the work into it loses the
  `feat`/`fix` history that review, bisect, and rollback rely on.

## How to apply this skill

When the user asks for a commit message:
1. Identify the type based on the actual change (read the diff if available).
2. Decide whether a scope is needed.
3. Write the description in imperative mood, short, no trailing period.
4. If the change breaks compatibility, mark it with `!` and add `BREAKING CHANGE:` in the footer.
5. One commit = one logical change; if the user describes several unrelated changes, suggest splitting into multiple commits.
6. If the change is a version bump (`chore(release)`), confirm it doesn't also carry feature/fix work — see "Release-alignment commits" above.
