# Foldville Instagram poster

Posts the reels and carousels in `schedule.json` to Instagram at their times (Polish time).
It runs by itself every 15 minutes on GitHub Actions and only acts when a post is due.

- **Pause:** set `"paused": true` in `config.json`.
- **What was posted:** `state.json` on the `state` branch.
- **Problems:** the poster opens an issue in this repository, so you get an email.
- **Key:** the repository secret `IG_TOKEN`. It lasts 60 days; you get an issue a week before it ends.
- **December sale line:** put the text in `"december_sale_line"` in `config.json` (empty = off).
