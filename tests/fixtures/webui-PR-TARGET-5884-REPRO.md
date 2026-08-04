Reproduction source: issue-artifact copied from https://github.com/nesquena/hermes-webui/issues/5884

1. Open `https://<your-hermes-host>/?q=hello+world`
2. Wait for the page to finish booting
3. Press Enter

Observed on current `origin/master` `d7c3c2b7`: the composer is prefilled, but the prompt is not submitted because `#msg` never took focus.
