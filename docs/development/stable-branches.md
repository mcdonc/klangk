# Stable Deployment Branches

For deployments that need a fixed release with cherry-picked hotfixes (rather than upgrading to the latest), create a stable branch:

```bash
git checkout -b stable/0.1 v0.1.0
git cherry-pick <hotfix-commit>
```

Tag patch releases on the stable branch:

```bash
git tag v0.1.1
git push origin v0.1.1
```

This triggers the release workflow, which builds and pushes versioned images. The deployment repo (e.g. `klangk-host-with-plugins`) references the tag via `KLANGK_REF=v0.1.1`.

Note: do not use `+` in tags — Docker image tags don't allow the `+` character.

Because the workspace base image is pinned in the Dockerfile, stable branches are isolated from base image changes on main. The branch only gets changes that are explicitly cherry-picked onto it.
