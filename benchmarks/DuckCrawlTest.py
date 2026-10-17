# DuckCrawlTest.py
"""
DuckCrawlTest.py — PointyHat search pipeline benchmark.
Uses DuckDuckGo (free, no API key) for search + Crawl4AI for scraping.
Produces identical metrics to BraveBenchmark.py for direct comparison.

Usage:
    python DuckCrawlTest.py
    python DuckCrawlTest.py --query "black fitted hat"
    python DuckCrawlTest.py --save

Run from ~/pointyhat/lambda/doJob/

Requirements:
    pip install duckduckgo-search crawl4ai boto3
    crawl4ai-setup   (installs Playwright/Chromium — run once)
    active CIC SSO session (for Sonnet recommendations)
"""

import json
import re
import sys
import os
import time
import asyncio
import argparse
import boto3
from datetime import datetime
from ddgs import DDGS
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy

# ============================================
# Config
# ============================================

PROFILE         = 'your-aws-profile'
REGION          = 'us-east-1'
SONNET_MODEL_ID = 'us.anthropic.claude-sonnet-4-5-20250929-v1:0'

TEST_QUERIES = [
    "black fitted hat",
    "pittsburgh pirates fitted",
    "gray timberwolves 9forty",
    "red snapback",
    "roberto clemente pirates fitted",
]

# CSS schema for generic product page extraction
# Crawl4AI will use this to pull structured data from hat product pages
PRODUCT_SCHEMA = {
    "name": "HatProduct",
    "baseSelector": "body",
    "fields": [
        {"name": "title",       "selector": "h1",                        "type": "text"},
        {"name": "price",       "selector": "[class*='price']",           "type": "text"},
        {"name": "description", "selector": "[class*='description'], [class*='product-detail']", "type": "text"},
        {"name": "image",       "selector": "meta[property='og:image']", "type": "attribute", "attribute": "content"},
        {"name": "og_title",    "selector": "meta[property='og:title']", "type": "attribute", "attribute": "content"},
    ],
}

# ============================================
# AWS clients
# ============================================

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
bedrock = session.client('bedrock-runtime')

# ============================================
# Step 1 — DuckDuckGo Search
# ============================================

def ddg_search(query: str, max_results: int = 20) -> list[dict]:
    """Search DuckDuckGo, return list of {url, title, description}."""
    try:
        ddgs = DDGS()
        results = list(ddgs.text(
            f"{query} hat buy",
            max_results=max_results,
            region='us-en',
        ))
        return results
    except Exception as e:
        print(f"  DDG search failed: {e}")
        return []


def extract_retailer(url: str) -> str:
    retailers = {
        'hatclub.com': 'Hat Club',
        'exclusivefitted.com': 'Exclusive Fitted',
        'topperzstore.com': 'Topperz',
        'hatstoreworld.com': 'Hat Store World',
        'fittedhats.com': 'Fitted Hats',
        'hatheaven.com': 'Hat Heaven',
        'projerseysports.com': 'Pro Jersey Sports',
        'shopjrsports.com': 'JR Sports',
        'ecapcity.com': 'ECap City',
        'crownminded.com': 'Crown Minded',
        'neweracap.com': 'New Era',
        '47brand.com': "'47 Brand",
        'lids.com': 'Lids',
        'fanatics.com': 'Fanatics',
        'footlocker.com': 'Foot Locker',
        'dickssportinggoods.com': "Dick's Sporting Goods",
        'rallyhouse.com': 'Rally House',
        'store.nba.com': 'NBA Store',
        'mlbshop.com': 'MLB Shop',
        'dtlr.com': 'DTLR',
        'amazon.com': 'Amazon',
    }
    for domain, name in retailers.items():
        if domain in url:
            return name
    return 'Other'


def is_hat_relevant(title: str, description: str, url: str = '') -> bool:
    keywords = [
        'hat', 'cap', 'fitted', 'snapback', 'beanie', '59fifty', '9fifty',
        '9forty', 'headwear', 'trucker', 'dad hat', 'bucket', 'new era',
        'baseball', 'mlb', 'nba', 'nfl', 'nhl', 'new-era', 'lids',
    ]
    text = (title + ' ' + description + ' ' + url).lower()
    return any(k in text for k in keywords)


def is_product_page(url: str):
    product_signals    = ['/products/', '/p/', '/item/', '/dp/']
    collection_signals = ['/collections/', '/category/', '/search', '/browse', 's?k=']
    url_lower = url.lower()
    if any(s in url_lower for s in product_signals):
        return True
    if any(s in url_lower for s in collection_signals):
        return False
    return None

# ============================================
# Step 2 — Crawl4AI Scraping
# ============================================

async def crawl_urls(urls: list[str]) -> list[dict]:
    """Crawl a list of URLs concurrently with Crawl4AI, extract product data."""
    extraction_strategy = JsonCssExtractionStrategy(PRODUCT_SCHEMA)
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        extraction_strategy=extraction_strategy,
        page_timeout=8000,       # 8s timeout per page
        wait_for_images=False,   # skip image loading for speed
    )
    browser_config = BrowserConfig(
        headless=True,
        verbose=False,
    )

    results = []
    async with AsyncWebCrawler(config=browser_config) as crawler:
        crawl_results = await crawler.arun_many(
            urls=urls,
            config=run_config,
        )
        for r in crawl_results:
            candidate = {
                'url':         r.url,
                'title':       '',
                'description': '',
                'retailer':    extract_retailer(r.url),
                'price':       None,
                'image':       None,
                'color_text':  None,
                'style':       None,
                'crawl_success': r.success,
            }

            if r.success and r.extracted_content:
                try:
                    data = json.loads(r.extracted_content)
                    if isinstance(data, list) and data:
                        data = data[0]
                    candidate['title']       = data.get('og_title') or data.get('title', '')
                    candidate['description'] = (data.get('description') or '')[:200]
                    candidate['image']       = data.get('image')
                    raw_price = data.get('price', '')
                    prices = re.findall(r'\$[\d,]+\.?\d*', str(raw_price))
                    if prices:
                        candidate['price'] = prices[0]
                except Exception:
                    # fall back to markdown title
                    if r.markdown:
                        lines = [l.strip() for l in r.markdown.split('\n') if l.strip()]
                        candidate['title'] = lines[0][:100] if lines else ''

            elif r.markdown:
                lines = [l.strip() for l in r.markdown.split('\n') if l.strip()]
                candidate['title'] = lines[0][:100] if lines else r.url.split('/')[-1]

            results.append(candidate)

    return results

# ============================================
# Step 3 — Recommendations (same as BraveBenchmark)
# ============================================

RECOMMENDATION_SYSTEM = """You are an expert hat curator. Select the top 3 hats from the candidates that best match the query.

- Use all candidate fields to evaluate fit
- Prefer candidates with has_image: true
- relevance: "high" = exact match, "medium" = close, "low" = partial
- Short reason per pick (1-2 sentences)
- Include overall reasoning

Respond ONLY with valid JSON:
{
  "recommendations": [
    {"title": "...", "url": "...", "retailer": "...", "price": "...", "reason": "...", "relevance": "high"}
  ],
  "reasoning": "..."
}"""

def get_recommendations(query: str, candidates: list[dict]) -> dict:
    candidates_text = json.dumps([{
        'title':       c.get('title', ''),
        'url':         c.get('url', ''),
        'retailer':    c.get('retailer', ''),
        'price':       c.get('price'),
        'description': c.get('description', '')[:150],
        'has_image':   c.get('image') is not None,
    } for c in candidates], indent=2)

    response = bedrock.converse(
        modelId=SONNET_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": f'Query: "{query}"\n\nCandidates:\n{candidates_text}'}]}],
        system=[{"text": RECOMMENDATION_SYSTEM}],
        inferenceConfig={"maxTokens": 2000, "temperature": 0}
    )
    text = response['output']['message']['content'][0]['text']
    text = re.sub(r'```json|```', '', text).strip()
    return json.loads(text)

# ============================================
# Single query benchmark
# ============================================

def run_benchmark(query: str) -> dict:
    result = {
        'query':     query,
        'timestamp': datetime.utcnow().isoformat(),
        'system':    'duckduckgo+crawl4ai',
        'timing':    {},
        'search':    {},
        'scraping':  {},
        'recommendations': {},
        'errors':    [],
    }

    t0 = time.time()

    # ── DDG Search ──
    t_search = time.time()
    ddg_results = ddg_search(query)
    result['timing']['ddg_search_s'] = round(time.time() - t_search, 2)

    # Filter to hat-relevant URLs
    urls = []
    for r in ddg_results:
        url   = r.get('href', '')
        title = r.get('title', '')
        body  = r.get('body', '')
        if url and is_hat_relevant(title, body, url):
            urls.append(url)

    result['search'] = {
        'total_ddg_results': len(ddg_results),
        'hat_relevant_urls': len(urls),
        'urls':              urls,
    }
    print(f"    DDG: {len(ddg_results)} results → {len(urls)} hat-relevant URLs")

    if not urls:
        result['errors'].append('No hat-relevant URLs from DDG')
        result['timing']['total_s'] = round(time.time() - t0, 2)
        return result

    # ── Crawl4AI Scraping ──
    t_crawl = time.time()
    try:
        candidates = asyncio.run(crawl_urls(urls))
        result['timing']['crawl4ai_s'] = round(time.time() - t_crawl, 2)

        successful   = [c for c in candidates if c.get('crawl_success')]
        with_image   = [c for c in candidates if c.get('image')]
        with_price   = [c for c in candidates if c.get('price')]
        product_pages = [c for c in candidates if is_product_page(c.get('url','')) is True]
        category_pages= [c for c in candidates if is_product_page(c.get('url','')) is False]

        result['scraping'] = {
            'candidates_total':  len(candidates),
            'crawl_successes':   len(successful),
            'crawl_failures':    len(candidates) - len(successful),
            'with_image':        len(with_image),
            'with_price':        len(with_price),
            'product_pages':     len(product_pages),
            'category_pages':    len(category_pages),
            'unknown_page_type': len(candidates) - len(product_pages) - len(category_pages),
            'retailers':         list({c.get('retailer','Other') for c in candidates}),
            'candidate_urls':    [c.get('url','') for c in candidates],
        }
        print(f"    Crawl4AI: {len(successful)}/{len(candidates)} successful, {len(with_image)} with image")

    except Exception as e:
        result['errors'].append(f"Crawl4AI error: {e}")
        result['timing']['total_s'] = round(time.time() - t0, 2)
        return result

    # ── Recommendations ──
    t_rec = time.time()
    try:
        recs = get_recommendations(query, candidates)
        result['timing']['recommendation_s'] = round(time.time() - t_rec, 2)
        result['timing']['total_s']           = round(time.time() - t0, 2)

        relevance_counts = {'high': 0, 'medium': 0, 'low': 0}
        rec_details = []
        for r in recs.get('recommendations', []):
            rel = r.get('relevance', 'unknown')
            relevance_counts[rel] = relevance_counts.get(rel, 0) + 1
            rec_details.append({
                'title':           r.get('title', ''),
                'retailer':        r.get('retailer', ''),
                'url':             r.get('url', ''),
                'price':           r.get('price'),
                'relevance':       rel,
                'is_product_page': is_product_page(r.get('url', '')),
                'reason':          r.get('reason', ''),
            })

        result['recommendations'] = {
            'count':            len(rec_details),
            'relevance_counts': relevance_counts,
            'results':          rec_details,
            'reasoning':        recs.get('reasoning', ''),
        }

    except Exception as e:
        result['errors'].append(f"Recommendation error: {e}")
        result['timing']['total_s'] = round(time.time() - t0, 2)

    return result

# ============================================
# Display — identical format to BraveBenchmark
# ============================================

DIVIDER  = '─' * 60
DIVIDER2 = '━' * 60

def print_result(r: dict):
    print(f"\n  {DIVIDER2}")
    print(f"  QUERY: \"{r['query']}\"")
    print(f"  {DIVIDER2}")

    if r['errors']:
        for e in r['errors']:
            print(f"  ⚠️  {e}")
        return

    s  = r['search']
    sc = r['scraping']
    rc = r['recommendations']
    t  = r['timing']

    print(f"\n  Search (DuckDuckGo)")
    print(f"    total results      : {s.get('total_ddg_results', 0)}")
    print(f"    hat-relevant URLs  : {s.get('hat_relevant_urls', 0)}")

    print(f"\n  Scraping (Crawl4AI)")
    print(f"    crawled            : {sc.get('candidates_total', 0)}")
    print(f"    successful         : {sc.get('crawl_successes', 0)}")
    print(f"    failed             : {sc.get('crawl_failures', 0)}")
    print(f"    with image         : {sc.get('with_image', 0)}")
    print(f"    with price         : {sc.get('with_price', 0)}")
    print(f"    product pages      : {sc.get('product_pages', 0)}")
    print(f"    category pages     : {sc.get('category_pages', 0)}")
    print(f"    retailers          : {', '.join(sorted(sc.get('retailers', [])))}")

    print(f"\n  Timing")
    print(f"    ddg search         : {t.get('ddg_search_s', '?')}s")
    print(f"    crawl4ai scraping  : {t.get('crawl4ai_s', '?')}s")
    print(f"    recommendation     : {t.get('recommendation_s', '?')}s")
    print(f"    ── TOTAL           : {t.get('total_s', '?')}s")

    print(f"\n  Top 3 Recommendations")
    for i, rec in enumerate(rc.get('results', []), 1):
        badge = {'high': '●', 'medium': '◑', 'low': '○'}.get(rec['relevance'], '?')
        page  = '📄' if rec['is_product_page'] else ('🗂' if rec['is_product_page'] is False else '❓')
        print(f"\n    [{i}] {badge} {rec['relevance'].upper()}  {page}")
        print(f"    {rec['title'][:60]}")
        print(f"    {rec['retailer']}  {rec['price'] or ''}")
        print(f"    {rec['url'][:80]}")
        print(f"    {rec['reason'][:100]}")

    rel = rc.get('relevance_counts', {})
    print(f"\n  Relevance: ● {rel.get('high',0)} high  ◑ {rel.get('medium',0)} medium  ○ {rel.get('low',0)} low")


def print_summary(results: list):
    print(f"\n\n  {DIVIDER2}")
    print(f"  BENCHMARK SUMMARY — DuckDuckGo + Crawl4AI system")
    print(f"  {DIVIDER2}")
    print(f"\n  {'Query':<40} {'URLs':>5} {'Crawl':>5} {'Imgs':>5} {'Prod':>5} {'Total':>8}  Relevance")
    print(f"  {DIVIDER}")
    for r in results:
        if r['errors'] and not r.get('scraping'):
            print(f"  {r['query']:<40} ERROR")
            continue
        s   = r['search']
        sc  = r['scraping']
        rc  = r['recommendations']
        t   = r['timing']
        rel = rc.get('relevance_counts', {})
        print(
            f"  {r['query']:<40}"
            f" {s.get('hat_relevant_urls',0):>5}"
            f" {sc.get('crawl_successes',0):>5}"
            f" {sc.get('with_image',0):>5}"
            f" {sc.get('product_pages',0):>5}"
            f" {t.get('total_s','?'):>7}s"
            f"  ●{rel.get('high',0)} ◑{rel.get('medium',0)} ○{rel.get('low',0)}"
        )
    print()

# ============================================
# Main
# ============================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--query', type=str, help='Run a single query')
    parser.add_argument('--save',  action='store_true', help='Save results to JSON')
    args = parser.parse_args()

    queries = [args.query] if args.query else TEST_QUERIES

    print(f"\n  PointyHat Benchmark — DuckDuckGo + Crawl4AI")
    print(f"  System: duckduckgo-search + crawl4ai")
    print(f"  Queries: {len(queries)}")
    print(f"  {DIVIDER}")

    all_results = []
    for i, query in enumerate(queries, 1):
        print(f"\n  [{i}/{len(queries)}] Running: \"{query}\"...")
        result = run_benchmark(query)
        print_result(result)
        all_results.append(result)

    if len(all_results) > 1:
        print_summary(all_results)

    if args.save:
        fname = f"benchmark_duckcrawl_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        with open(fname, 'w') as f:
            json.dump(all_results, f, indent=2, default=str)
        print(f"  Results saved to {fname}\n")

if __name__ == '__main__':
    main()
