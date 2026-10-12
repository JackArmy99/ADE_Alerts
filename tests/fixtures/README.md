# Test fixtures

The files here are SYNTHETIC: this repo was built from a cloud container that cannot reach Instagram or
amsterdam-dance-event.nl. They mimic the structure the parsers expect.

Replace / extend them with real captures from Jack's laptop (CLAUDE.md: build against real responses):

- `ade-radar ig-inspect` saves the live Following feed to `instagram/live_<timestamp>/`
  (feed.html, feed.png, articles.json = raw DOM extraction, parsed.json = what the parser made of it).
- `ade-radar ade-discover` saves the ADE filter page and every JSON response to `ade/discover_<timestamp>/`.

When a live capture parses differently from these fixtures, add it as a test case and fix the parser.
