"""python -m pensieve.streams check [--output measurements.json]. Uses the guarded discover fetcher."""

import argparse
import asyncio
import json
from pathlib import Path

from pensieve.streams import load_catalog
from pensieve.streams.preview import preview


async def check(output):
    catalog = load_catalog()
    results = []
    for url in dict.fromkeys(source.url for stream in catalog.streams for source in stream.sources):
        result = await preview(url, use_cache=False)
        results.append(
            {
                key: result[key]
                for key in ("url", "feed_url", "posts_per_week", "sample_size", "measured_at", "error")
            }
        )
        print(f"{url}: {result['error'] or str(result['posts_per_week']) + ' posts/week'}")
    Path(output).write_text(
        json.dumps({"catalog_version": catalog.version, "sources": results}, indent=2) + "\n"
    )
    return int(any(row["error"] for row in results))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check"])
    parser.add_argument("--output", default="stream-measurements.json")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(check(args.output)))


if __name__ == "__main__":
    main()
