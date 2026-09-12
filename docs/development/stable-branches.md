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
- Backports are sync-only (#3456): the backport pull request opens under
  the workflow's PAT (`BACKPORT_TOKEN`) and merges itself (squash) in
  the same run — the cherry-pick is content `main`'s CI already
  passed. Every pull-request-triggered workflow targets `main`, so the
  backport pull request runs no CI, and `stable/2.0` carries no
  required status checks — nothing gates the merge and nothing waits.
  A conflicted cherry-pick opens a draft pull request; the merge step
  fails loudly on it, a human resolves the conflicts, marks it ready,
  and merges it by hand.
- A `/backport` comment on an already-merged pull request runs the same
  flow — the rescue path for a merge that happened without the label.

After v2.0.0 ships, remove the variable (`gh variable delete
BACKPORT_2_0_DEFAULT`). Backports become opt-in: apply the
`backport/2.0` label before merging, or comment `/backport` on the
merged pull request afterwards.

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
