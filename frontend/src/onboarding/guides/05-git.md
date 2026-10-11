# Git branches for stories

Bug Hunter can create a feature branch in a GitHub repository for a **User Story**, and remove exactly that branch again. This is optional and off until an administrator sets it up. Pull requests, merges and tags are not part of it.

## One-time setup (admin or manager)

1. Edit the project (click it in the sidebar) and open **Git integration**.
2. Enter the API base URL, the organization and the default base branch.
3. Paste a project token. Use a fine-grained token with Metadata read and Contents read and write. It is stored encrypted and never shown again.
4. Click **Save Git settings**, then **Test Connection**, and **Load repositories** to check the token can see them.

## Create a branch

1. Open a **Story**. The Git branches section is only on Stories; other types are refused.
2. Choose the repository and the base branch.
3. Click **Create Feature Branch**.

The name is generated for you as `feature_<story number>_<title slug>`, where the slug comes from the first 25 characters of the title. Asking twice does not create a second branch.

## Remove a branch

Any member of the story's project can remove a branch that Bug Hunter created, if the administrator enabled deletion. Protected names such as `main`, `master`, `dev`, `develop` and `release`, and the project's base branch, are always refused. If the branch is already gone at the provider it counts as removed. Removed branches stay visible as history.

## If something fails

Authentication, TLS, timeout and rate-limit errors leave the branch record active, so you can retry. The message tells you which kind it was.
