# yt-runner

Public orchestration repository for the YouTube Content Factory. Keep this repository limited to the two workflow files. All business logic and history live in the private `yt-core` repository.

## One-time setup

1. Create a private repository named `yt-core` and import the `yt-core` zip.
2. Replace `your-username/yt-core` in both workflow files with the real private repository path.
3. Create a fine-grained PAT with access only to `yt-core` and **Contents: Read and write**. Add it here as `PRIVATE_REPO_PAT`.
4. Add the API secrets listed in the private repo README to this repository's Actions secrets — this now includes optional `GEMINI_API_KEY` (LLM fallback), `ELEVENLABS_API_KEY`/`FISH_API_KEY` (voice fallback), and the per-channel `CHANNELx_YT_TOKEN` + `YOUTUBE_CLIENT_SECRET_JSON` secrets, which `trigger-window.yml` now also uses to check for due thumbnail A/B-test rotations every hour.
5. Run the `Video Pipeline` workflow manually once with `channel=channel1` after configuring the private repo.

The trigger workflow dispatches the video workflow using the built-in GitHub token, so the private-repo PAT does not need write access to this public repository.

Required Telegram secrets are `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. The chat ID is used as an allowlist so approval commands from other chats are ignored.

## Security boundary

- There is intentionally no `pull_request` or `pull_request_target` trigger.
- Never add secrets or pipeline code here.
- Review workflow changes before merging them.
