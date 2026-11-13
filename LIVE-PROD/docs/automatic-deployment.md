# Automatic deployment to EC2

Push to **develop** to run `.github/workflows/deploy-ec2.yml`:

1. GitHub runs the full offline regression suite on Python 3.11.
2. The deployment job connects to EC2 using a dedicated restricted SSH key.
3. EC2 fetches the exact tested develop commit and builds from its Git snapshot
   while the current application remains running.
4. The new API image runs the application sync tests with networking disabled.
5. EC2 fast-forward pulls develop and activates the tested images.
6. API and Streamlit health checks must pass. Failed activation restores the
   previous images and Compose configuration.

Pull requests targeting develop run tests only. Pushes to other branches do not
deploy. Deployment jobs are serialized and older superseded commits are skipped.
To retry a failed run, open **Actions → Test and deploy IRS PIN → Re-run jobs**.

## Credentials

These repository Actions secrets are required:

- `IRS_EC2_HOST`
- `IRS_EC2_USER`
- `IRS_EC2_SSH_KEY`
- `IRS_EC2_KNOWN_HOSTS`

The deployment key is separate from the general EC2 login key. Its server-side
authorized_keys entry uses `restrict` and a forced command:
`/usr/local/sbin/irs-pin-github-deploy`. Only `deploy <full commit SHA>` is accepted,
and that SHA must match the current origin/develop. The entrypoint is installed
root-owned from `scripts/github_deploy_entrypoint.py`; subsequent release helpers
are loaded from the verified Git commit. Host-key checking stays enabled.

Application and Graph credentials remain in the server's ignored `.env`; they
are not uploaded to GitHub. Build contexts exclude `.env` and mutable data.

## Persistent state and recovery

The project remains `live-prod`, with API port 8002 and frontend port 8520.
Both services retain `/home/ubuntu/IRSRequesterAutomation/LIVE-PROD/data` as their
data mount. Release snapshots and rollback metadata are under
`/home/ubuntu/.local/state/irs-pin-deployments/actions/`; `active.json` identifies
the last successful Compose configuration and revision.

Deployment never runs git reset/clean or discards operator source edits. It stops
on local source/staged changes, a non-fast-forward update, incoming credentials,
or commits that modify/delete tracked runtime databases. Existing audit changes
are allowed when the commit leaves those paths untouched. In particular, review
the pre-existing staged repository cleanup before pushing its database removals;
those require preserving/untracking the live data separately.

The application waits for a responsive health endpoint before activation and
allows up to 60 minutes for graceful container shutdown so batch work can finish.
The Actions deployment job has a two-hour limit. Old image tags and release
snapshots are retained for recovery; deployment stops below 2 GiB free disk space.
Review this app's retained releases/images periodically; never prune other apps.

After an activation rollback, Git may already point at the attempted commit while
the old images remain live. The Actions run fails and rollback metadata records
the active configuration. Resolve the failure and rerun or push a fix.
