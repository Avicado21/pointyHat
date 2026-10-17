# BraveCrawlTest.py
"""
BraveCrawlTest.py — PointyHat search pipeline benchmark.
Uses Brave Search for URL discovery + Crawl4AI for scraping.
Isolates the scraper variable — same search as BraveBenchmark.py,
replaces urllib/og:image/Shopify JSON with Crawl4AI browser scraping.

Usage:
    python BraveCrawlTest.py
    python BraveCrawlTest.py --query "black fitted hat"
    python BraveCrawlTest.py --save

Run from ~/pointyhat/lambda/doJob/ or ~/pointyhat/lambda/hacksearch/

Requirements:
    pip install crawl4ai boto3 pydantic
    crawl4ai-setup   (installs Playwright/Chromium — run once)
    active CIC SSO session + SSM /pointyhat/brave-api-key
"""

import json
import re
import sys
import os
import time
import asyncio
import argparse
import gzip
import urllib.request
import urllib.parse
import boto3
from datetime import datetime
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'hacksearch'))
import hacksearch as hs

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

# CSS schema for product page extraction
PRODUCT_SCHEMA = {
    "name": "HatProduct",
    "baseSelector": "body",
    "fields": [
        {"name": "title",       "selector": "h1",                                           "type": "text"},
        {"name": "price",       "selector": "[class*='price'], [class*='Price']",            "type": "text"},
        {"name": "description", "selector": "[class*='description'], [class*='product-detail'], [class*='product-info']", "type": "text"},
        {"name": "image",       "selector": "meta[property='og:image']",                    "type": "attribute", "attribute": "content"},
        {"name": "og_title",    "selector": "meta[property='og:title']",                    "type": "attribute", "attribute": "content"},
        {"name": "og_desc",     "selector": "meta[property='og:description']",              "type": "attribute", "attribute": "content"},
    ],
}

# ============================================
# AWS clients
# ============================================

session    = boto3.Session(profile_name=PROFILE, region_name=REGION)
hs.ssm     = session.client('ssm')
bedrock    = session.client('bedrock-runtime')

# ============================================
# Step 1 — Brave Search (same as BraveBenchmark)
# ============================================

def brave_search_urls(query: str, api_key: str, count: int = 20) -> list[str]:
    """Run Brave search, return list of URLs."""
    intent = hs.interpret_query(query)
    queries = [intent.search_query] + intent.search_variants
    seen = set()
    urls = []
    for q in queries:
        for r in hs.brave_search(q, api_key, count=count):
            url = r.get('url', '')
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    print(f"  Brave: {len(urls)} unique URLs across {len(queries)} queries")
    return urls, intent


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
    """Crawl URLs concurrently with Crawl4AI, extract product data."""
    extraction_strategy = JsonCssExtractionStrategy(PRODUCT_SCHEMA)
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        extraction_strategy=extraction_strategy,
        page_timeout=8000,
        wait_for_images=False,
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
                'url':           r.url,
                'title':         '',
                'description':   '',
                'retailer':      hs.extract_retailer(r.url),
                'price':         None,
                'image':         None,
                'color_text':    None,
                'style':         None,
                'crawl_success': r.success,
            }

            if r.success and r.extracted_content:
                try:
                    data = json.loads(r.extracted_content)
                    if isinstance(data, list) and data:
                        data = data[0]
                    candidate['title']       = data.get('og_title') or data.get('title', '')
                    candidate['description'] = (data.get('og_desc') or data.get('description') or '')[:200]
                    candidate['image']       = data.get('image')
                    raw_price = data.get('price', '')
                    prices = re.findall(r'\$[\d,]+\.?\d*', str(raw_price))
                    if prices:
                        candidate['price'] = prices[0]
                except Exception:
                    if r.markdown:
                        lines = [l.strip() for l in r.markdown.split('\n') if l.strip()]
                        candidate['title'] = lines[0][:100] if lines else ''
            elif r.markdown:
                lines = [l.strip() for l in r.markdown.split('\n') if l.strip()]
                candidate['title'] = lines[0][:100] if lines else r.url.split('/')[-1]

            results.append(candidate)

    return results

# ============================================
# Step 3 — Recommendations (same as both benchmarks)
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
        'timestamp': datetime.now(datetime.UTC).isoformat() if hasattr(datetime, 'UTC') else datetime.utcnow().isoformat(),
        'system':    'brave+crawl4ai',
        'timing':    {},
        'search':    {},
        'scraping':  {},
        'recommendations': {},
        'errors':    [],
    }

    t0 = time.time()

    # ── Brave Search ──
    try:
        api_key = hs.get_brave_api_key()
    except Exception as e:
        result['errors'].append(f"SSM error: {e}")
        return result

    t_search = time.time()
    urls, intent = brave_search_urls(query, api_key)
    result['timing']['brave_search_s'] = round(time.time() - t_search, 2)

    # Filter with same hat-relevance check as BraveBenchmark
    hat_urls = []
    raw_results_map = {}
    for url in urls:
        title_guess = url.split('/')[-1].replace('-', ' ')
        if hs.is_hat_relevant(title_guess, ''):
            hat_urls.append(url)

    # Also add Shopify direct search URLs
    t_shopify = time.time()
    existing = set(urls)
    shopify_candidates = hs.shopify_direct_search(intent.search_query, existing)
    shopify_urls = [c['url'] for c in shopify_candidates]
    all_urls = list(dict.fromkeys(urls + shopify_urls))  # dedup, preserve order
    result['timing']['shopify_search_s'] = round(time.time() - t_shopify, 2)

    result['search'] = {
        'brave_urls':   len(urls),
        'shopify_urls': len(shopify_urls),
        'total_urls':   len(all_urls),
        'intent': {
            'colors':   intent.colors,
            'styles':   intent.styles,
            'variants': intent.search_variants,
        },
    }
    print(f"  Total URLs to crawl: {len(all_urls)}")

    if not all_urls:
        result['errors'].append('No URLs found')
        result['timing']['total_s'] = round(time.time() - t0, 2)
        return result

    # ── Crawl4AI Scraping ──
    t_crawl = time.time()
    try:
        candidates = asyncio.run(crawl_urls(all_urls))
        result['timing']['crawl4ai_s'] = round(time.time() - t_crawl, 2)

        successful    = [c for c in candidates if c.get('crawl_success')]
        with_image    = [c for c in candidates if c.get('image')]
        with_price    = [c for c in candidates if c.get('price')]
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
        print(f"  Crawl4AI: {len(successful)}/{len(candidates)} successful, {len(with_image)} with image, {len(with_price)} with price")

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
# Display
# ============================================

DIVIDER  = '─' * 60
DIVIDER2 = '━' * 60

def print_result(r: dict):
    print(f"\n  {DIVIDER2}")
    print(f"  QUERY: \"{r['query']}\"")
    print(f"  {DIVIDER2}")

    if r['errors'] and not r.get('scraping'):
        for e in r['errors']:
            print(f"  ⚠️  {e}")
        return

    s  = r['search']
    sc = r['scraping']
    rc = r['recommendations']
    t  = r['timing']

    print(f"\n  Search (Brave)")
    print(f"    brave URLs         : {s.get('brave_urls', 0)}")
    print(f"    shopify URLs       : {s.get('shopify_urls', 0)}")
    print(f"    total              : {s.get('total_urls', 0)}")
    print(f"    colors             : {s.get('intent',{}).get('colors',[])}")
    print(f"    styles             : {s.get('intent',{}).get('styles',[])}")

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
    print(f"    brave search       : {t.get('brave_search_s', '?')}s")
    print(f"    shopify search     : {t.get('shopify_search_s', '?')}s")
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

    if r.get('errors'):
        for e in r['errors']:
            print(f"  ⚠️  {e}")


def print_summary(results: list):
    print(f"\n\n  {DIVIDER2}")
    print(f"  BENCHMARK SUMMARY — Brave + Crawl4AI system")
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
            f" {s.get('total_urls',0):>5}"
            f" {sc.get('crawl_successes',0):>5}"
            f" {sc.get('with_image',0):>5}"
            f" {sc.get('product_pages',0):>5}"
            f" {t.get('total_s','?'):>7}s"
            f"  ●{rel.get('high',0)} ◑{rel.get('medium',0)} ○{rel.get('low',0)}"
        )
    print()


# ============================================
# Comparison helper — print all 3 systems side by side
# ============================================

def compare_with_baselines(brave_results: list, duck_results: list, brave_crawl_results: list):
    print(f"\n\n  {DIVIDER2}")
    print(f"  THREE-WAY COMPARISON")
    print(f"  {'':40}  {'Brave+urllib':>14}  {'DDG+Crawl4AI':>14}  {'Brave+Crawl4AI':>16}")
    print(f"  {DIVIDER2}")

    for i, query in enumerate(TEST_QUERIES):
        b  = brave_results[i]     if i < len(brave_results)      else {}
        d  = duck_results[i]      if i < len(duck_results)        else {}
        bc = brave_crawl_results[i] if i < len(brave_crawl_results) else {}

        b_rel  = b.get('recommendations',{}).get('relevance_counts',{})
        d_rel  = d.get('recommendations',{}).get('relevance_counts',{})
        bc_rel = bc.get('recommendations',{}).get('relevance_counts',{})

        print(f"\n  {query}")
        print(
            f"  {'time':<12}"
            f"  {str(b.get('timing',{}).get('total_s','?'))+'s':>14}"
            f"  {str(d.get('timing',{}).get('total_s','?'))+'s':>14}"
            f"  {str(bc.get('timing',{}).get('total_s','?'))+'s':>16}"
        )
        print(
            f"  {'images':<12}"
            f"  {b.get('hacksearch',{}).get('with_image','?'):>14}"
            f"  {d.get('scraping',{}).get('with_image','?'):>14}"
            f"  {bc.get('scraping',{}).get('with_image','?'):>16}"
        )
        print(
            f"  {'prices':<12}"
            f"  {b.get('hacksearch',{}).get('with_price','?'):>14}"
            f"  {d.get('scraping',{}).get('with_price','?'):>14}"
            f"  {bc.get('scraping',{}).get('with_price','?'):>16}"
        )
        print(
            f"  {'prod pages':<12}"
            f"  {b.get('hacksearch',{}).get('product_pages','?'):>14}"
            f"  {d.get('scraping',{}).get('product_pages','?'):>14}"
            f"  {bc.get('scraping',{}).get('product_pages','?'):>16}"
        )
        print(
            f"  {'relevance':<12}"
            f"  ●{b_rel.get('high',0)} ◑{b_rel.get('medium',0)} ○{b_rel.get('low',0)}        "
            f"  ●{d_rel.get('high',0)} ◑{d_rel.get('medium',0)} ○{d_rel.get('low',0)}        "
            f"  ●{bc_rel.get('high',0)} ◑{bc_rel.get('medium',0)} ○{bc_rel.get('low',0)}"
        )
    print()

# ============================================
# Main
# ============================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--query', type=str, help='Run a single query')
    parser.add_argument('--save',  action='store_true', help='Save results to JSON')
    parser.add_argument('--compare', nargs=2, metavar=('BRAVE_JSON', 'DUCK_JSON'),
                        help='Compare with saved BraveBenchmark and DuckCrawlTest JSON files')
    args = parser.parse_args()

    queries = [args.query] if args.query else TEST_QUERIES

    print(f"\n  PointyHat Benchmark — Brave + Crawl4AI")
    print(f"  System: brave-search + crawl4ai")
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
        fname = f"benchmark_bravecrawl_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        with open(fname, 'w') as f:
            json.dump(all_results, f, indent=2, default=str)
        print(f"  Results saved to {fname}\n")

    # Optional three-way comparison
    if args.compare:
        brave_json, duck_json = args.compare
        try:
            with open(brave_json) as f:
                brave_results = json.load(f)
            with open(duck_json) as f:
                duck_results = json.load(f)
            compare_with_baselines(brave_results, duck_results, all_results)
        except Exception as e:
            print(f"  Could not load comparison files: {e}")

if __name__ == '__main__':
    main()
