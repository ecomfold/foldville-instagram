# Foldville Instagram poster

Posts the reels and carousels in `schedule.json` to Instagram at their times (Polish time).
It runs by itself every 15 minutes on GitHub Actions and only acts when a post is due.

- **Pause:** set `"paused": true` in `config.json`.
- **What was posted:** `state.json` on the `state` branch.
- **Problems:** the poster opens an issue in this repository, so you get an email.
- **Key:** the repository secret `IG_TOKEN`. It doesn't expire; if Instagram ever rejects it, you get an issue with the steps for a new one.
- **Trial Reels:** Instagram allows them from about 200 followers. Until then the Trial slots post as normal reels (`"trial_fallback": "normal"`, or `"skip"` to leave them out), and the poster tries Trial again once a day, so it switches back by itself.
- **December sale line:** put the text in `"december_sale_line"` in `config.json` (empty = off).
