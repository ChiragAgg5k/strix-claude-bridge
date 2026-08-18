# README media

- `hero.svg`: project-owned architecture/wordmark artwork.
- `live-run-receipt.svg` / `.png`: project-owned visual receipt derived from the final live Team-backed scan's metadata-only evidence.
- `team-status.gif` / `.png`: sanitized official `claude auth status` capture; email, organization ID, and organization name are omitted.
- `e2e-demo.gif` / `.png`: real credential-free bridge run through Strix root/child coordination, production Strix Docker, finding/report/SARIF generation, and cleanup. Inference is scripted in this recording; a separate live Team-backed combined run is documented in [`../live-verification.md`](../live-verification.md).
- `view-demo.png`: screenshot of the Strix local viewer opened against a bridge-generated run.
- `tapes/*.tape`: VHS sources. `e2e-demo.tape` calls `scripts/readme_demo.sh`, which copies the fixture into a temporary directory before scanning.

Regenerate from the repository root:

```bash
vhs docs/assets/tapes/team-status.tape
vhs docs/assets/tapes/e2e-demo.tape
ffmpeg -sseof -0.2 -i docs/assets/team-status.gif -frames:v 1 docs/assets/team-status.png -y
ffmpeg -sseof -0.2 -i docs/assets/e2e-demo.gif -frames:v 1 docs/assets/e2e-demo.png -y
rsvg-convert -w 1400 -h 620 docs/assets/live-run-receipt.svg -o docs/assets/live-run-receipt.png
```

The Team-status tape requires an official local Claude login. Do not add identity fields or credential material to public assets.
