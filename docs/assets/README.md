# README media

- `hero.svg`: project-owned architecture/wordmark artwork.
- `team-status.gif` / `.png`: sanitized official `claude auth status` capture; email, organization ID, and organization name are omitted.
- `e2e-demo.gif` / `.png`: real credential-free bridge run through Strix root/child coordination, production Strix Docker, finding/report/SARIF generation, and cleanup. Inference is scripted in this recording; a separate live Team-backed combined run is documented in [`../live-verification.md`](../live-verification.md).
- `tapes/*.tape`: VHS sources. `e2e-demo.tape` calls `scripts/readme_demo.sh`, which copies the fixture into a temporary directory before scanning.

Regenerate from the repository root:

```bash
vhs docs/assets/tapes/team-status.tape
vhs docs/assets/tapes/e2e-demo.tape
ffmpeg -sseof -0.2 -i docs/assets/team-status.gif -frames:v 1 docs/assets/team-status.png -y
ffmpeg -sseof -0.2 -i docs/assets/e2e-demo.gif -frames:v 1 docs/assets/e2e-demo.png -y
```

The Team-status tape requires an official local Claude login. Do not add identity fields or credential material to public assets.
