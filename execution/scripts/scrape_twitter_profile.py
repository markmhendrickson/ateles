#!/usr/bin/env python3
"""
Scrape recent tweets from a Twitter/X profile using Playwright.
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# Add repo root to path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Error: playwright not installed. Install with: pip install playwright")
    print("Then run: playwright install chromium")
    sys.exit(1)


async def scrape_twitter_profile(username: str, max_tweets: int = 10):
    """Scrape recent tweets from a Twitter/X profile.

    Args:
        username: Twitter username (without @)
        max_tweets: Maximum number of tweets to scrape

    Returns:
        List of tweet dictionaries
    """
    tweets = []

    async with async_playwright() as p:
        # Launch browser
        print(f"Launching browser to scrape @{username}...")

        # Try to load saved auth state
        auth_state_dir = REPO_ROOT / "playwright" / ".auth"
        auth_state_dir.mkdir(parents=True, exist_ok=True)
        auth_state_path = auth_state_dir / "twitter_auth_state.json"

        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        # Load saved auth state if available (fall back to fresh context on error)
        context = None
        if auth_state_path.exists():
            try:
                print("Loading saved authentication state...")
                context = await browser.new_context(
                    storage_state=str(auth_state_path),
                    viewport={"width": 1920, "height": 1080},
                )
            except Exception as e:
                print(f"Auth state load failed ({e}), using fresh context.")
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080}
                )
        if context is None:
            print(
                "No saved auth state. Browser will open; log in to X.com if prompted."
            )
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080}
            )

        page = await context.new_page()

        # Navigate to profile
        profile_url = f"https://x.com/{username}"
        print(f"Navigating to {profile_url}...")

        try:
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(8)  # Let timeline start loading

            # Check if login is required
            page_content = await page.content()
            if (
                "hasn't posted" in page_content
                or "Sign up" in page_content
                or "Log in" in page_content
            ):
                if not auth_state_path.exists():
                    print("\n" + "=" * 60)
                    print("Authentication required!")
                    print("Please log in to X.com in the browser window.")
                    print("After logging in, the script will continue automatically.")
                    print("=" * 60 + "\n")

                    # Wait for user to log in (check every 5 seconds)
                    for i in range(60):  # Wait up to 5 minutes
                        await asyncio.sleep(5)
                        current_url = page.url
                        if (
                            f"x.com/{username}" in current_url
                            or f"twitter.com/{username}" in current_url
                        ):
                            # Check if we can see tweets now
                            test_content = await page.content()
                            if (
                                "hasn't posted" not in test_content
                                or 'data-testid="tweet"' in test_content
                            ):
                                print(
                                    "✓ Login detected! Waiting for timeline to load..."
                                )
                                await asyncio.sleep(3)
                                break
                        print(f"Waiting for login... ({i+1}/60)")

                    # Save auth state after successful login
                    await context.storage_state(path=str(auth_state_path))
                    print("✓ Saved authentication state for future use")

            # Wait for at least one tweet to appear (timeline can be slow)
            try:
                await page.wait_for_selector(
                    'article[data-testid="tweet"]', timeout=15000
                )
                await asyncio.sleep(3)
            except Exception:
                await asyncio.sleep(5)  # Extra time if selector never appears

            # Debug: save HTML to inspect DOM
            tmp_dir = REPO_ROOT / "data" / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            html_path = tmp_dir / f"twitter_profile_{username}.html"
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(await page.content())
            print(f"HTML saved to: {html_path}")
            # Screenshot
            screenshot_path = tmp_dir / f"twitter_profile_{username}.png"
            await page.screenshot(path=str(screenshot_path))
            print(f"Screenshot saved to: {screenshot_path}")

            # Scroll to load more tweets (scale scrolls with max_tweets: ~1 scroll per 3 tweets)
            scroll_count = max(5, min(40, (max_tweets // 3) + 5))
            print(
                f"Scrolling {scroll_count} times to load up to {max_tweets} tweets..."
            )
            for i in range(scroll_count):
                await page.evaluate("window.scrollBy(0, 800)")
                await asyncio.sleep(1.5)
                if (i + 1) % 10 == 0:
                    print(f"  Scroll {i+1}/{scroll_count}")

            # Extract tweet data via JavaScript (more reliable across X.com DOM changes)
            slice_limit = max(max_tweets, 150)
            print("Extracting tweet data...")
            raw_tweets = await page.evaluate(
                f"""
                () => {{
                    const articles = document.querySelectorAll('article[data-testid="tweet"]');
                    if (!articles.length) return [];
                    return Array.from(articles).slice(0, {slice_limit}).map(art => {{
                        const textEl = art.querySelector('[data-testid="tweetText"]');
                        const timeEl = art.querySelector('time');
                        const linkEl = art.querySelector('a[href*="/status/"]');
                        const href = linkEl ? linkEl.getAttribute('href') : '';
                        const tweetId = href ? href.split('/status/')[1]?.split('?')[0]?.split('/')[0] : '';
                        const replyEl = art.querySelector('[data-testid="reply"]');
                        const retweetEl = art.querySelector('[data-testid="retweet"]');
                        const likeEl = art.querySelector('[data-testid="like"]');
                        const num = (el) => {{ if (!el) return 0; const t = el.textContent || ''; const n = t.replace(/\\D/g,''); return n ? parseInt(n, 10) : 0; }};
                        return {{
                            text: textEl ? textEl.innerText : '',
                            timestamp: timeEl ? timeEl.getAttribute('datetime') : null,
                            url: href ? 'https://x.com' + (href.startsWith('/') ? href : '/' + href) : '',
                            tweet_id: tweetId,
                            replies: num(replyEl),
                            retweets: num(retweetEl),
                            likes: num(likeEl)
                        }};
                    }});
                }}
            """
            )

            if raw_tweets:
                for i, r in enumerate(raw_tweets[:max_tweets]):
                    tweets.append(
                        {
                            "tweet_id": r.get("tweet_id", ""),
                            "username": username,
                            "text": r.get("text", ""),
                            "url": r.get("url", ""),
                            "timestamp": r.get("timestamp"),
                            "replies": r.get("replies", 0),
                            "retweets": r.get("retweets", 0),
                            "likes": r.get("likes", 0),
                            "scraped_at": datetime.now().isoformat(),
                        }
                    )
                    text = r.get("text", "")
                    print(
                        f"  {i+1}. {text[:70]}..."
                        if len(text) > 70
                        else f"  {i+1}. {text}"
                    )

            # Fallback: Python selector extraction when JS got nothing
            if not tweets:
                tweet_articles = await page.query_selector_all(
                    'article[data-testid="tweet"]'
                )
                if not tweet_articles:
                    tweet_articles = await page.query_selector_all(
                        'article[role="article"]'
                    )
                print(f"Fallback: found {len(tweet_articles)} tweet elements")
                for i, article in enumerate(tweet_articles[:max_tweets]):
                    if i >= max_tweets:
                        break
                    try:
                        text_element = await article.query_selector(
                            '[data-testid="tweetText"]'
                        )
                        text = await text_element.inner_text() if text_element else ""
                        time_element = await article.query_selector("time")
                        timestamp = (
                            await time_element.get_attribute("datetime")
                            if time_element
                            else None
                        )
                        link_elements = await article.query_selector_all(
                            'a[href*="/status/"]'
                        )
                        tweet_url, tweet_id = "", ""
                        for link in link_elements:
                            href = await link.get_attribute("href")
                            if href and "/status/" in href:
                                tweet_url = (
                                    f"https://x.com{href}"
                                    if href.startswith("/")
                                    else href
                                )
                                parts = href.split("/status/")
                                if len(parts) > 1:
                                    tweet_id = parts[1].split("?")[0].split("/")[0]
                                break
                        reply_el = await article.query_selector('[data-testid="reply"]')
                        retweet_el = await article.query_selector(
                            '[data-testid="retweet"]'
                        )
                        like_el = await article.query_selector('[data-testid="like"]')

                        async def num(el):
                            if not el:
                                return 0
                            t = await el.inner_text()
                            return int("".join(filter(str.isdigit, t or "")) or 0)

                        replies = await num(reply_el)
                        retweets = await num(retweet_el)
                        likes = await num(like_el)
                        tweet = {
                            "tweet_id": tweet_id,
                            "username": username,
                            "text": text,
                            "url": tweet_url,
                            "timestamp": timestamp,
                            "replies": replies,
                            "retweets": retweets,
                            "likes": likes,
                            "scraped_at": datetime.now().isoformat(),
                        }
                        tweets.append(tweet)
                        print(
                            f"  {i+1}. {text[:60]}..."
                            if len(text) > 60
                            else f"  {i+1}. {text}"
                        )
                    except Exception as e:
                        print(f"  Error extracting tweet {i+1}: {e}")

            # Fetch full tweet text by visiting each tweet's page (timeline often truncates)
            # Skip when scraping many tweets (>30) to avoid long runtime
            fetch_full = tweets and len(tweets) <= 30
            if fetch_full:
                print("Fetching full tweet content...")
                for i, tweet in enumerate(tweets):
                    url = tweet.get("url", "")
                    if not url:
                        continue
                    try:
                        await page.goto(
                            url, wait_until="domcontentloaded", timeout=15000
                        )
                        await asyncio.sleep(2)
                        full_text = await page.evaluate(
                            """
                            () => {
                                const el = document.querySelector('[data-testid="tweetText"]');
                                return el ? el.innerText : '';
                            }
                        """
                        )
                        if full_text and len(full_text) > len(tweet.get("text", "")):
                            tweet["text"] = full_text
                            print(
                                f"    Full text for tweet {i+1}: {len(full_text)} chars"
                            )
                    except Exception as e:
                        print(f"    Could not fetch full text for tweet {i+1}: {e}")
            elif tweets:
                print("Skipping full tweet fetch (many tweets); using timeline text.")

            # If we successfully scraped tweets and didn't have auth state, save it now
            if tweets and not auth_state_path.exists():
                print("Saving authentication state for future use...")
                await context.storage_state(path=str(auth_state_path))

        except Exception as e:
            print(f"Error navigating to profile: {e}")

        finally:
            await browser.close()

    return tweets


async def main():
    """Main entry point."""
    username = sys.argv[1] if len(sys.argv) > 1 else "markymark"
    max_tweets = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    print(f"Scraping last {max_tweets} tweets from @{username}...")
    tweets = await scrape_twitter_profile(username, max_tweets)

    # Always save to tmp as JSON (even if empty)
    output_dir = REPO_ROOT / "data" / "tmp"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"twitter_{username}_tweets.json"

    payload = {
        "username": username,
        "scraped_at": datetime.now().isoformat(),
        "tweet_count": len(tweets),
        "tweets": tweets,
    }
    with open(output_file, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"\nSaved to: {output_file}")

    if tweets:
        print(f"Successfully scraped {len(tweets)} tweets:")
        print(json.dumps(tweets, indent=2))
    else:
        print("No tweets extracted (file saved with empty tweets list)")


if __name__ == "__main__":
    asyncio.run(main())
