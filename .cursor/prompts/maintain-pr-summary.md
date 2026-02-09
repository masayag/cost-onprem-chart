# Maintain PR Summary

This prompt helps maintain a running PR summary document that tracks changes as work is developed on a branch.

## Usage

Type `@maintain-pr-summary` in Cursor, then:
- "Update the PR summary with the changes we just made"
- "Create a new PR summary for this branch"
- "Add the bug fix to the PR summary"

## Options

### Default Behavior
- **Location**: Current working directory (project root)
- **Filename**: `PR-SUMMARY-<branch-name>.md` (e.g., `PR-SUMMARY-testetson22-flpath-3075.md`)
- **Gitignored**: Yes (file is automatically ignored)

### Custom Location
Specify a full file path to save the summary elsewhere:
- "Update the PR summary at `/Users/me/workspaces/my-pr-summary.md`"
- "Create PR summary in the workspaces folder"

## What Gets Tracked

The PR summary document includes:

### Header
- Branch name and target branch
- Date of last update
- High-level summary of changes

### Sections
1. **Summary** - Brief description of the PR's purpose
2. **Key Changes** - Bullet points of major changes
3. **New Files** - Table of files created with line counts
4. **Modified Files** - Table of files changed with descriptions
5. **Deleted Files** - Table of files removed with reasons
6. **Bug Fixes** - Specific bugs addressed
7. **Test Results** - Latest test run output
8. **Architecture** - Diagrams if applicable
9. **Related Documents** - Links to relevant docs

## Commands

### Create New Summary
```
Create a PR summary for the current branch
```

### Update Existing Summary
```
Update the PR summary with the changes we just made to test_reports.py
```

### Add Specific Section
```
Add a bug fix section to the PR summary for the database name issue
```

### Generate from Git Diff
```
Generate PR summary from git diff against main
```

## Template Structure

```markdown
# PR Summary: <Title>

**Branch**: `<branch-name>`  
**Target**: `main`  
**Last Updated**: <date>

---

## Summary

<Brief description of what this PR accomplishes>

### Key Changes

- Change 1
- Change 2
- Change 3

---

## New Files

| File | Lines | Purpose |
|------|-------|---------|
| `path/to/file.py` | 100 | Description |

## Modified Files

| File | Change |
|------|--------|
| `path/to/file.py` | Description of changes |

## Deleted Files

| File | Reason |
|------|--------|
| `path/to/file.py` | Why it was removed |

---

## Bug Fixes

### Issue Title
**Problem**: Description of the bug
**Solution**: How it was fixed

---

## Test Results

```
<Latest test output>
```

---

## Related Documents

- [Document 1](./path/to/doc.md)
- [Document 2](./path/to/doc.md)
```

## Notes

- The summary file is gitignored by default to avoid cluttering PRs
- Use the workspaces folder for summaries you want to persist across sessions
- The summary can be copied into the actual PR description when ready
- Updates are additive - new changes are appended to existing sections
