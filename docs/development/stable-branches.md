# Stable Deployment Branches

## Backports to stable/2.0 (2.0 pre-release window)

While 2.0 is in development, changes that must ship in v2.0.0 land on both
`main` and `stable/2.0` (releases tag from the stable branch). The
backport runs as automation (#3361):

- Every pull request opened against `main` carries the `backport/2.0`
  label automatically while the repository variable
  `BACKPORT_2_0_DEFAULT` is set to `true`. Remove the label before
  merging to keep a change on `main` only.
- Merging a labeled pull request cherry-picks its squash commit onto
  `stable/2.0` and opens a backport pull request
  (`.github/workflows/backport.yml`, the `korthout/backport-action`
  workflow). A cherry-pick that conflicts opens a draft pull request
  that carries the first conflict committed; resolve the conflicts on
  its branch, then mark it ready and merge it.
- Only the `backport/2.0` label and the `/backport` comment select the
  backport; the target is fixed to `stable/2.0` and other labels play
  no part.
- While `BACKPORT_2_0_AUTO_MERGE` is set to `true`, the backport pull
  request merges itself (squash) as soon as it applies cleanly — the
  commit is a cherry-pick of an already-reviewed `main` commit. The
  backport pull request carries no test runs: it is opened with the
  workflow's `GITHUB_TOKEN`, and pull requests and pushes caused by
  that token start no CI workflows. `stable/2.0` carries no required
  status checks, so nothing gates the merge. At the freeze, drop
  `BACKPORT_2_0_AUTO_MERGE` and merge backports by hand, or pass the
  workflow a PAT (its `github_token` input) so its pull requests run
  CI and required checks on `stable/2.0` can gate the merge.
- A `/backport` comment on an already-merged pull request runs the same
  flow — the rescue path for a merge that happened without the label.

After v2.0.0 ships, remove both variables (`gh variable delete
BACKPORT_2_0_DEFAULT`, `gh variable delete BACKPORT_2_0_AUTO_MERGE`).
Backports become opt-in: apply the `backport/2.0` label before merging,
or comment `/backport` on the merged pull request afterwards.

## Hand-fed stable branches

For deployments that need a fixed release with cherry-picked hotfixes
(rather than upgrading to the latest), create a stable branch:

```bash
git checkout -b stable/0.1 v0.1.0
git cherry-pick <hotfix-commit>
```

Tag patch releases on the stable branch:

```bash
git tag v0.1.1
git push origin v0.1.1
```

This triggers the release workflow, which builds and pushes versioned images. A downstream deployment fork pins a version by checking out the tag (`git checkout v0.1.1`) before building the host image.

Note: do not use `+` in tags — Docker image tags don't allow the `+` character.

Because the workspace base image is pinned in the Dockerfile, stable branches are isolated from base image changes on main. The branch only gets changes that are explicitly cherry-picked onto it.
