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

## Historical note

`chore(release)` version-bump commits were used before dynamic versioning; versions are now computed at build/CI time from git history (see the `version-standard` skill), so no such commit is needed.

## How to apply this skill

When the user asks for a commit message:
1. Identify the type based on the actual change (read the diff if available).
2. Decide whether a scope is needed.
3. Write the description in imperative mood, short, no trailing period.
4. If the change breaks compatibility, mark it with `!` and add `BREAKING CHANGE:` in the footer.
5. One commit = one logical change; if the user describes several unrelated changes, suggest splitting into multiple commits.
