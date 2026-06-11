# Stable Deployment Branches

For deployments that need a fixed release with cherry-picked hotfixes (rather than upgrading to the latest), create a stable branch:

```bash
git checkout -b stable/2026.06.10 v2026.06.10
git cherry-pick <hotfix-commit>
```

Tag backport releases with a `-N` suffix to distinguish them from same-day mainline releases:

```bash
git tag v2026.06.10-1
git push origin v2026.06.10-1
```

This triggers the release workflow, which builds and pushes versioned images. The deployment repo (e.g. `klangk-host-with-plugins`) references the tag via `KLANGK_REF=v2026.06.10-1`.

Note: do not use `+` in tags — Docker image tags don't allow the `+` character.

Because the workspace base image is pinned in the Dockerfile, stable branches are isolated from base image changes on main. The branch only gets changes that are explicitly cherry-picked onto it.
