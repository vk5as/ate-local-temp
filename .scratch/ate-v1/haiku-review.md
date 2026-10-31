# Haiku review of a packet

Haiku is one of the three parallel reviewers. Follow `.scratch/ate-v1/review-packet.md`: same checklist, same reply format.

What's different for Haiku:

- No worktree and no test runs. Work from `/home/dev/projects/ate-ornith` using only read commands:

  ```
  git log --oneline integration/ate-v1..<branch>
  git diff --stat integration/ate-v1...<branch>
  git diff integration/ate-v1...<branch> -- <path>
  git show <branch>:<path>
  git show integration/ate-v1:<spec path> | sed -n 'A,Bp'
  ```

- Never check out, switch, edit, commit or create anything.
- If the branch is behind `integration/ate-v1`, files that changed only on the integration branch show up in a two-dot diff. Use the three-dot diff (`...`), and don't report those files as deleted.
