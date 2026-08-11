# Changelogs

This folder contains changelog files that describe changes to be released.

## Adding a changelog

Run `changelogs add` to create a new changelog file.

Pull requests with package or test changes must commit a changelog file. For an
intentional no-release change, run `changelogs add --empty` instead. README,
repository metadata, and GitHub workflow-only changes do not require a changelog.

## File format

Changelog files are markdown with YAML frontmatter:

```markdown
---
package-name: minor
other-package: patch
---

Description of the changes made.
```

## Releasing

Run `changelogs version` to apply version bumps and generate changelogs.
