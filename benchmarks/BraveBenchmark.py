# BraveBenchmark.py
"""
benchmark.py — PointyHat search pipeline benchmark.
Runs 5 test queries through the current hacksearch (Brave) pipeline
and records result quality metrics for comparison against future systems.

Usage:
    python benchmark.py
    python benchmark.py --query "black fitted hat"  # single query

Run from ~/pointyhat/lambda/doJob/
Requires: active CIC SSO session, SSM /pointyhat/brave-api-key
"""

import json
import re
import sys
import os
import time
import argparse
import boto3
from datetime import datetime

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

# ============================================
# AWS clients
# ============================================

session    = boto3.Session(profile_name=PROFILE, region_name=REGION)
hs.ssm     = session.client('ssm')
bedrock    = session.client('bedrock-runtime')

# ============================================
# Recommendation call
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

def get_recommendations(query: str, hacksearch_data: dict) -> dict:
    intent     = hacksearch_data.get('intent', {})
    candidates = hacksearch_data.get('candidates', [])

    candidates_text = json.dumps([{
        'title':       c.get('title', ''),
        'url':         c.get('url', ''),
        'retailer':    c.get('retailer', ''),
        'price':       c.get('price'),
        'description': c.get('description', '')[:150],
        'color_text':  (c.get('color_text') or '')[:80],
        'style':       c.get('style'),
        'has_image':   c.get('image') is not None,
    } for c in candidates], indent=2)

    prompt = f"""Query: "{query}"
Intent — colors: {intent.get('colors', [])} styles: {intent.get('styles', [])} team: {intent.get('team', 'none')}

Candidates:
{candidates_text}"""

    response = bedrock.converse(
        modelId=SONNET_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        system=[{"text": RECOMMENDATION_SYSTEM}],
        inferenceConfig={"maxTokens": 2000, "temperature": 0}
    )
    text = response['output']['message']['content'][0]['text']
    text = re.sub(r'```json|```', '', text).strip()
    return json.loads(text)

# ============================================
# Single query benchmark
# ============================================

def is_product_page(url: str) -> bool:
    """Heuristic: does the URL look like a product page vs category/collection?"""
    product_signals = ['/products/', '/p/', '/item/', '/dp/']
    collection_signals = ['/collections/', '/category/', '/search', '/browse', 's?k=']
    url_lower = url.lower()
    if any(s in url_lower for s in product_signals):
        return True
    if any(s in url_lower for s in collection_signals):
        return False
    return None  # unknown

def run_benchmark(query: str) -> dict:
    result = {
        'query':               query,
        'timestamp':           datetime.utcnow().isoformat(),
        'system':              'brave+urllib',
        'timing': {},
        'hacksearch': {},
        'recommendations': {},
        'errors': [],
    }

    # ── HackSearch ──
    t0 = time.time()
    try:
        intent = hs.interpret_query(query)
        api_key = hs.get_brave_api_key()

        t_search_start = time.time()
        raw = hs.search_all_variants(intent, api_key)
        result['timing']['brave_search_s'] = round(time.time() - t_search_start, 2)

        candidates = hs.parse_results(raw)
        existing = {c['url'] for c in candidates}

        t_shopify_start = time.time()
        for c in hs.shopify_direct_search(intent.search_query, existing):
            candidates.append(c)
            existing.add(c['url'])
        result['timing']['shopify_search_s'] = round(time.time() - t_shopify_start, 2)

        if len(candidates) < 5:
            for r in hs.brave_search(query, api_key):
                url = r.get('url', '')
                if url and url not in existing:
                    parsed = hs.parse_results([r])
                    if parsed:
                        candidates.extend(parsed)
                        existing.add(url)

        t_filter_start = time.time()
        before_filter = len(candidates)
        candidates = hs.filter_dead_urls(candidates)
        result['timing']['url_filter_s'] = round(time.time() - t_filter_start, 2)

        t_image_start = time.time()
        candidates = hs.fetch_images(candidates, api_key)
        result['timing']['image_fetch_s'] = round(time.time() - t_image_start, 2)

        result['timing']['hacksearch_total_s'] = round(time.time() - t0, 2)

        # candidate metrics
        with_image    = [c for c in candidates if c.get('image')]
        with_price    = [c for c in candidates if c.get('price')]
        product_pages = [c for c in candidates if is_product_page(c.get('url','')) is True]
        category_pages= [c for c in candidates if is_product_page(c.get('url','')) is False]

        result['hacksearch'] = {
            'intent': {
                'colors':   intent.colors,
                'styles':   intent.styles,
                'variants': intent.search_variants,
            },
            'candidates_total':    len(candidates),
            'candidates_before_filter': before_filter,
            'filtered_out':        before_filter - len(candidates),
            'with_image':          len(with_image),
            'with_price':          len(with_price),
            'product_pages':       len(product_pages),
            'category_pages':      len(category_pages),
            'unknown_page_type':   len(candidates) - len(product_pages) - len(category_pages),
            'retailers':           list({c.get('retailer','Other') for c in candidates}),
            'candidate_urls':      [c.get('url','') for c in candidates],
        }

        hacksearch_data = {
            'candidates': [hs.HatCandidate(**c).model_dump() for c in candidates],
            'intent':     intent.model_dump(),
        }

    except Exception as e:
        result['errors'].append(f"HackSearch error: {e}")
        result['timing']['hacksearch_total_s'] = round(time.time() - t0, 2)
        return result

    # ── Recommendations ──
    t_rec = time.time()
    try:
        recs = get_recommendations(query, hacksearch_data)
        result['timing']['recommendation_s'] = round(time.time() - t_rec, 2)
        result['timing']['total_s']           = round(time.time() - t0, 2)

        relevance_counts = {'high': 0, 'medium': 0, 'low': 0}
        rec_details = []
        for r in recs.get('recommendations', []):
            rel = r.get('relevance', 'unknown')
            relevance_counts[rel] = relevance_counts.get(rel, 0) + 1
            is_product = is_product_page(r.get('url', ''))
            rec_details.append({
                'title':      r.get('title', ''),
                'retailer':   r.get('retailer', ''),
                'url':        r.get('url', ''),
                'price':      r.get('price'),
                'relevance':  rel,
                'is_product_page': is_product,
                'reason':     r.get('reason', ''),
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
    q  = r['query']
    hs = r['hacksearch']
    rc = r['recommendations']
    t  = r['timing']

    print(f"\n  {DIVIDER2}")
    print(f"  QUERY: \"{q}\"")
    print(f"  {DIVIDER2}")

    if r['errors']:
        for e in r['errors']:
            print(f"  ⚠️  {e}")
        return

    # intent
    print(f"\n  Intent")
    print(f"    colors   : {hs.get('intent',{}).get('colors', [])}")
    print(f"    styles   : {hs.get('intent',{}).get('styles', [])}")
    print(f"    variants : {hs.get('intent',{}).get('variants', [])}")

    # candidate metrics
    print(f"\n  Candidates")
    print(f"    total          : {hs.get('candidates_total', 0)}")
    print(f"    filtered out   : {hs.get('filtered_out', 0)}")
    print(f"    with image     : {hs.get('with_image', 0)}")
    print(f"    with price     : {hs.get('with_price', 0)}")
    print(f"    product pages  : {hs.get('product_pages', 0)}")
    print(f"    category pages : {hs.get('category_pages', 0)}")
    print(f"    retailers      : {', '.join(sorted(hs.get('retailers', [])))}")

    # timing
    print(f"\n  Timing")
    print(f"    brave search   : {t.get('brave_search_s', '?')}s")
    print(f"    shopify search : {t.get('shopify_search_s', '?')}s")
    print(f"    url filter     : {t.get('url_filter_s', '?')}s")
    print(f"    image fetch    : {t.get('image_fetch_s', '?')}s")
    print(f"    hacksearch     : {t.get('hacksearch_total_s', '?')}s")
    print(f"    recommendation : {t.get('recommendation_s', '?')}s")
    print(f"    ── TOTAL       : {t.get('total_s', '?')}s")

    # top 3
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
    print(f"  BENCHMARK SUMMARY — Brave + urllib system")
    print(f"  {DIVIDER2}")
    print(f"\n  {'Query':<40} {'Cands':>5} {'Imgs':>5} {'Prod':>5} {'Total':>8}  Relevance")
    print(f"  {DIVIDER}")
    for r in results:
        if r['errors']:
            print(f"  {r['query']:<40} ERROR")
            continue
        hs  = r['hacksearch']
        rc  = r['recommendations']
        t   = r['timing']
        rel = rc.get('relevance_counts', {})
        print(
            f"  {r['query']:<40}"
            f" {hs.get('candidates_total',0):>5}"
            f" {hs.get('with_image',0):>5}"
            f" {hs.get('product_pages',0):>5}"
            f" {t.get('total_s','?'):>7}s"
            f"  ●{rel.get('high',0)} ◑{rel.get('medium',0)} ○{rel.get('low',0)}"
        )
    print()

# ============================================
# Main
# ============================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--query', type=str, help='Run a single query instead of all 5')
    parser.add_argument('--save', action='store_true', help='Save results to benchmark_results.json')
    args = parser.parse_args()

    queries = [args.query] if args.query else TEST_QUERIES

    print(f"\n  PointyHat Benchmark — Brave + urllib")
    print(f"  System: hacksearch.py")
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
        fname = f"benchmark_brave_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        with open(fname, 'w') as f:
            json.dump(all_results, f, indent=2, default=str)
        print(f"  Results saved to {fname}\n")

if __name__ == '__main__':
    main()
