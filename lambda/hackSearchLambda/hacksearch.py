# hacksearch.py
import json
import boto3
import urllib.request
import urllib.parse
import gzip
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pydantic import BaseModel
from typing import Optional

# ============================================
# AWS Clients
# ============================================

ssm = boto3.client('ssm', region_name='us-east-1')

BRAVE_API_KEY_PARAM = '/pointyhat/brave-api-key'

# ============================================
# Pydantic Models
# ============================================

class SearchIntent(BaseModel):
    search_query: str
    search_variants: list[str] = []
    team: Optional[str] = None
    colors: list[str] = []
    styles: list[str] = []
    design_keywords: list[str] = []
    price_max: Optional[float] = None

class HatCandidate(BaseModel):
    title: str
    url: str
    retailer: str
    price: Optional[str] = None
    description: str = ''
    image: Optional[str] = None
    color_text: Optional[str] = None
    style: Optional[str] = None

class HackSearchResponse(BaseModel):
    candidates: list[HatCandidate]
    intent: SearchIntent

# ============================================
# SSM
# ============================================

def get_brave_api_key() -> str:
    response = ssm.get_parameter(
        Name=BRAVE_API_KEY_PARAM,
        WithDecryption=True
    )
    return response['Parameter']['Value']

# ============================================
# Step 1 — Query Interpretation
# Simple keyword extraction — no Claude call.
# Full semantic interpretation happens in Worker.
# ============================================

VALID_STYLES = ['fitted', 'snapback', 'beanie', 'trucker', 'dad hat', 'a-frame', 'other']

STYLE_SYNONYMS = {
    '59fifty': 'fitted', '5950': 'fitted',
    '9fifty': 'snapback', '950': 'snapback',
    '9forty': 'a-frame', '940': 'a-frame', 'af940': 'a-frame',
    '9twenty': 'dad hat', '920': 'dad hat',
}

# Maps query color words -> canonical color name
COLOR_SYNONYMS = {
    'black': 'black', 'blackout': 'black',
    'white': 'white', 'cream': 'white', 'ivory': 'white', 'natural': 'white',
    'gray': 'gray', 'grey': 'gray', 'graphite': 'gray', 'charcoal': 'gray', 'slate': 'gray',
    'navy': 'navy', 'midnight': 'navy',
    'royal blue': 'royal blue', 'royal': 'royal blue',
    'blue': 'blue', 'cobalt': 'blue',
    'red': 'red', 'scarlet': 'red', 'crimson': 'red', 'cardinal': 'red',
    'maroon': 'maroon', 'burgundy': 'maroon', 'oxblood': 'maroon', 'wine': 'maroon',
    'green': 'green', 'kelly': 'green', 'forest': 'green',
    'olive': 'olive',
    'gold': 'gold', 'golden': 'gold', 'yellow': 'gold',
    'purple': 'purple', 'violet': 'purple',
    'orange': 'orange',
    'brown': 'brown', 'tan': 'brown', 'khaki': 'brown',
    'pink': 'pink', 'rose': 'pink',
    'teal': 'teal', 'aqua': 'teal', 'turquoise': 'teal',
    'camo': 'camo', 'camouflage': 'camo',
    'corduroy': 'corduroy',
}

def interpret_query(query: str) -> SearchIntent:
    """
    Lightweight query parser — no LLM call.
    Extracts colors and styles via keyword matching, builds search variants.
    Worker's Sonnet call handles full semantic understanding.
    """
    q = query.lower()

    # Detect colors — check multi-word synonyms first, then single words
    colors = []
    matched_spans = []
    for synonym in sorted(COLOR_SYNONYMS.keys(), key=len, reverse=True):
        idx = q.find(synonym)
        if idx == -1:
            continue
        end = idx + len(synonym)
        # skip if overlaps with an already matched span
        if any(s <= idx < e or s < end <= e for s, e in matched_spans):
            continue
        canonical = COLOR_SYNONYMS[synonym]
        if canonical not in colors:
            colors.append(canonical)
        matched_spans.append((idx, end))

    # Detect styles
    styles = []
    for synonym, canonical in STYLE_SYNONYMS.items():
        if synonym in q and canonical not in styles:
            styles.append(canonical)
    for style in VALID_STYLES:
        if style in q and style not in styles:
            styles.append(style)

    # Build search variants from color + style combinations
    search_variants = []
    for color in colors[:2]:
        for style in styles[:2]:
            variant = f"{query} {color} {style}".strip()
            if variant != query and variant not in search_variants:
                search_variants.append(variant)
    # Color-only or style-only variants if no combinations
    if not search_variants:
        for color in colors[:2]:
            variant = f"{query} {color}".strip()
            if variant != query and variant not in search_variants:
                search_variants.append(variant)
        for style in styles[:2]:
            variant = f"{query} {style}".strip()
            if variant != query and variant not in search_variants:
                search_variants.append(variant)

    return SearchIntent(
        search_query=query,
        search_variants=search_variants,
        colors=colors,
        styles=styles,
    )

# ============================================
# Step 2 — Brave Search
# ============================================

def brave_search(query: str, api_key: str, count: int = 20) -> list[dict]:
    try:
        encoded = urllib.parse.quote(f"{query} hat buy")
        url = f"https://api.search.brave.com/res/v1/web/search?q={encoded}&count={count}&search_lang=en"
        req = urllib.request.Request(url)
        req.add_header('Accept', 'application/json')
        req.add_header('Accept-Encoding', 'gzip')
        req.add_header('X-Subscription-Token', api_key)
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            if r.info().get('Content-Encoding') == 'gzip':
                raw = gzip.decompress(raw)
            data = json.loads(raw.decode('utf-8'))
            return data.get('web', {}).get('results', [])
    except Exception as e:
        print(f"Brave search failed for '{query}': {e}")
        return []

def search_all_variants(intent: SearchIntent, api_key: str) -> list[dict]:
    all_results = []
    seen_urls = set()
    queries = [intent.search_query] + intent.search_variants

    for query in queries:
        print(f"Searching: {query}")
        for r in brave_search(query, api_key):
            url = r.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_results.append(r)

    print(f"Total unique Brave results: {len(all_results)}")
    return all_results

# ============================================
# Step 2b — Shopify Direct Search
# ============================================

SHOPIFY_SEARCH_STORES = [
    'www.hatclub.com',
    'exclusivefitted.com',
    'www.topperzstore.com',
    'www.topperzstoreusa.com',
    'projerseysports.com',
    'www.hatstoreworld.com',
    'www.fittedhats.com',
    'www.hatheaven.com',
    'crownminded.com',
    'shopjrsports.com',
]

def shopify_search(store_domain: str, query: str) -> list[str]:
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://{store_domain}/search?q={encoded}&type=product"
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36')
        req.add_header('Accept', 'text/html')
        with urllib.request.urlopen(req, timeout=4) as r:
            html = r.read(100000).decode('utf-8', errors='ignore')
        urls = re.findall(r'href="(/products/[^"?#]+)"', html)
        unique_paths = list(dict.fromkeys(urls))[:5]
        return [f"https://{store_domain}{p}" for p in unique_paths]
    except Exception as e:
        print(f"Shopify search failed for {store_domain}: {e}")
        return []

def shopify_direct_search(query: str, existing_urls: set) -> list[dict]:
    new_candidates = []

    def search_store(store):
        results = []
        for url in shopify_search(store, query):
            if url not in existing_urls:
                results.append({
                    'url': url,
                    'title': url.split('/products/')[-1].replace('-', ' ').title(),
                    'description': '',
                    'retailer': extract_retailer(url),
                    'price': None,
                    'image': None,
                    'color_text': None,
                    'style': None,
                })
        return results

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(search_store, store): store for store in SHOPIFY_SEARCH_STORES}
        for future in as_completed(futures):
            try:
                new_candidates.extend(future.result())
            except Exception as e:
                print(f"Shopify store future failed: {e}")

    print(f"Shopify direct search found {len(new_candidates)} additional candidates")
    return new_candidates

# ============================================
# Step 3 — Parse Raw Results
# ============================================

def extract_retailer(url: str) -> str:
    retailers = {
        'hatclub.com': 'Hat Club',
        'exclusivefitted.com': 'Exclusive Fitted',
        'topperzstore.com': 'Topperz',
        'topperzstoreusa.com': 'Topperz USA',
        'hatstoreworld.com': 'Hat Store World',
        'fittedhats.com': 'Fitted Hats',
        'hatheaven.com': 'Hat Heaven',
        'projerseysports.com': 'Pro Jersey Sports',
        'shopjrsports.com': 'JR Sports',
        'ecapcity.com': 'ECap City',
        'crownminded.com': 'Crown Minded',
        'neweracap.com': 'New Era',
        '47brand.com': "'47 Brand",
        'mitchellandness.com': 'Mitchell & Ness',
        'lids.com': 'Lids',
        'fanatics.com': 'Fanatics',
        'footlocker.com': 'Foot Locker',
        'champssports.com': 'Champs Sports',
        'hibbett.com': 'Hibbett',
        'dickssportinggoods.com': "Dick's Sporting Goods",
        'rallyhouse.com': 'Rally House',
        'store.nba.com': 'NBA Store',
        'mlbshop.com': 'MLB Shop',
        'shop.nhl.com': 'NHL Shop',
        'dtlr.com': 'DTLR',
        'nike.com': 'Nike',
        'adidas.com': 'Adidas',
        'ebay.com': 'eBay',
        'amazon.com': 'Amazon',
    }
    for domain, name in retailers.items():
        if domain in url:
            return name
    return 'Other'

def extract_price(text: str) -> Optional[str]:
    matches = re.findall(r'\$[\d,]+\.?\d*', text)
    if matches:
        prices = [float(m.replace('$', '').replace(',', '')) for m in matches]
        return f"${min(prices):.2f}"
    return None

def is_hat_relevant(title: str, description: str) -> bool:
    hat_keywords = [
        'hat', 'cap', 'fitted', 'snapback', 'beanie', '59fifty', '5950',
        '9fifty', '9forty', '9twenty', 'headwear', 'trucker', 'dad hat',
        'bucket hat', 'visor', 'strapback', 'flexfit', 'new era',
        'mitchell', '47 brand', 'adjustable', 'af940',
    ]
    text = (title + ' ' + description).lower()
    return any(kw in text for kw in hat_keywords)

def parse_results(raw_results: list[dict]) -> list[dict]:
    candidates = []
    for item in raw_results:
        url = item.get('url', '')
        title = item.get('title', '')
        description = item.get('description', '')
        if not is_hat_relevant(title, description):
            continue
        candidates.append({
            'title': title,
            'url': url,
            'description': description,
            'retailer': extract_retailer(url),
            'price': extract_price(description + title),
            'image': None,
            'color_text': None,
            'style': None,
        })
    return candidates

# ============================================
# Step 4 — Filter Dead URLs
# ============================================

TRUST_BLINDLY_DOMAINS = [
    'lids.com', 'fanatics.com', 'store.nba.com', 'mlbshop.com',
    'shop.nhl.com', 'dickssportinggoods.com', 'champssports.com', 'footlocker.com',
]

PRODUCT_SIGNALS = [
    '/products/', '/p/', '-hat', '-cap', '-fitted', '-snapback',
    '/mlb-', '/nba-', '/nfl-', '/nhl-', '/mens-', '/womens-',
]

def is_url_alive(url: str) -> bool:
    if any(d in url for d in TRUST_BLINDLY_DOMAINS):
        return any(s in url.lower() for s in PRODUCT_SIGNALS)
    try:
        req = urllib.request.Request(url, method='HEAD')
        req.add_header('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36')
        req.add_header('Accept', 'text/html,application/xhtml+xml')
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status in [200, 301, 302]
    except urllib.error.HTTPError as e:
        return e.code in [403, 405, 406, 429]
    except:
        return False

def filter_dead_urls(candidates: list[dict]) -> list[dict]:
    alive_map = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(is_url_alive, c['url']): c['url'] for c in candidates}
        for future in as_completed(futures):
            url = futures[future]
            try:
                alive_map[url] = future.result()
            except:
                alive_map[url] = False

    alive = [c for c in candidates if alive_map.get(c['url'], False)]
    print(f"URL filter: {len(candidates)} → {len(alive)} alive")
    return alive

# ============================================
# Step 5 — Image Fetching (no validation)
# ============================================

SHOPIFY_STORE_DOMAINS = [
    'hatclub.com', 'exclusivefitted.com', 'hatheaven.com',
    'fittedhats.com', 'projerseysports.com', 'topperzstore.com',
    'topperzstoreusa.com', 'hatstoreworld.com', '47brand.com',
    'crownminded.com', 'shopjrsports.com', 'ecapcity.com',
    'neweracap.com', 'dtlr.com',
]

def fetch_shopify_product_data(url: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    try:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc
        if not any(store in domain for store in SHOPIFY_STORE_DOMAINS):
            return None, None, None
        path = parsed.path
        if '/products/' not in path:
            return None, None, None
        handle = path.split('/products/')[-1].split('?')[0].strip('/')
        if not handle:
            return None, None, None

        api_url = f"https://{domain}/products/{handle}.json"
        req = urllib.request.Request(api_url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        req.add_header('Accept', 'application/json')
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())

        product = data.get('product', {})
        images = product.get('images', [])
        image = images[0].get('src') if images else None
        variants = product.get('variants', [])
        prices = [float(v['price']) for v in variants if v.get('price')]
        price = f"${min(prices):.2f}" if prices else None
        options = product.get('options', [])
        color_option = next((o for o in options if o.get('name', '').lower() == 'color'), None)
        color_values = ' '.join(color_option.get('values', [])) if color_option else ''
        title = product.get('title', '')
        tags = ' '.join(product.get('tags', []))
        color_text = f"{title} {color_values} {tags}".lower().strip()

        return image, price, color_text
    except Exception as e:
        print(f"Shopify API failed for {url}: {e}")
        return None, None, None

class OGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.og_image = None
        self.og_title = None

    def handle_starttag(self, tag, attrs):
        if tag == 'meta':
            attrs_dict = dict(attrs)
            if attrs_dict.get('property') == 'og:image':
                self.og_image = attrs_dict.get('content')
            if attrs_dict.get('property') == 'og:title':
                self.og_title = attrs_dict.get('content')

def fetch_og_data(url: str) -> tuple[Optional[str], Optional[str]]:
    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36')
        req.add_header('Accept', 'text/html,application/xhtml+xml')
        with urllib.request.urlopen(req, timeout=3) as r:
            html = r.read(80000).decode('utf-8', errors='ignore')
        parser = OGParser()
        parser.feed(html)
        color_text = (parser.og_title or '').lower()
        return parser.og_image, color_text
    except Exception as e:
        print(f"OG fetch failed for {url}: {e}")
        return None, None

def brave_image_fallback(title: str, api_key: str) -> Optional[str]:
    try:
        encoded = urllib.parse.quote(f"{title} hat product")
        url = f"https://api.search.brave.com/res/v1/images/search?q={encoded}&count=3"
        req = urllib.request.Request(url)
        req.add_header('Accept', 'application/json')
        req.add_header('Accept-Encoding', 'gzip')
        req.add_header('X-Subscription-Token', api_key)
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            if r.info().get('Content-Encoding') == 'gzip':
                raw = gzip.decompress(raw)
            data = json.loads(raw.decode('utf-8'))
            results = data.get('results', [])
            return results[0].get('thumbnail', {}).get('src') if results else None
    except Exception as e:
        print(f"Brave image fallback failed: {e}")
        return None

def fetch_image_for_candidate(candidate: dict) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Returns (url, image, price, color_text)."""
    url = candidate['url']

    image, price, color_text = fetch_shopify_product_data(url)
    if image:
        return url, image, price, color_text

    og_image, og_color = fetch_og_data(url)
    return url, og_image, None, og_color

def fetch_images(candidates: list[dict], api_key: str) -> list[dict]:
    # Fetch Shopify/OG images concurrently
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_image_for_candidate, c): c for c in candidates}
        for future in as_completed(futures):
            c = futures[future]
            try:
                url, image, price, color_text = future.result()
                c['image'] = image
                if price and not c['price']:
                    c['price'] = price
                if color_text:
                    c['color_text'] = color_text
            except Exception as e:
                print(f"Image fetch failed: {e}")
                c['image'] = None

    # Brave fallback for anything still missing
    no_image = [c for c in candidates if not c.get('image')]
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(brave_image_fallback, c['title'], api_key): c for c in no_image}
        for future in as_completed(futures):
            c = futures[future]
            try:
                image = future.result()
                if image:
                    c['image'] = image
            except:
                pass

    return candidates

# ============================================
# Lambda Handler
# ============================================

def lambda_handler(event, context):
    query = event.get('query', '').strip()
    if not query:
        return {'statusCode': 400, 'error': 'No query provided'}

    try:
        api_key = get_brave_api_key()

        # Step 1 — lightweight query interpretation (no LLM)
        intent = interpret_query(query)
        print(f"Intent: {intent.search_query} | styles={intent.styles}")

        # Step 2 — Brave search across all variants
        raw_results = search_all_variants(intent, api_key)

        # Step 3 — parse and filter hat-relevant results
        candidates = parse_results(raw_results)
        print(f"After parse: {len(candidates)} candidates")

        # Step 3b — Shopify direct search
        existing_urls = {c['url'] for c in candidates}
        shopify_candidates = shopify_direct_search(intent.search_query, existing_urls)
        for c in shopify_candidates:
            candidates.append(c)
            existing_urls.add(c['url'])
        print(f"After Shopify direct: {len(candidates)} candidates")

        # Step 3c — progressive relaxation if sparse
        if len(candidates) < 5:
            print(f"Only {len(candidates)} candidates — relaxing query")
            for r in brave_search(query, api_key):
                url = r.get('url', '')
                if url and url not in existing_urls:
                    parsed = parse_results([r])
                    if parsed:
                        candidates.extend(parsed)
                        existing_urls.add(url)
            print(f"After relaxation: {len(candidates)} candidates")

        # Step 4 — filter dead URLs
        candidates = filter_dead_urls(candidates)

        # Step 5 — fetch images (no validation)
        candidates = fetch_images(candidates, api_key)
        print(f"Final candidate count: {len(candidates)}")

        response = HackSearchResponse(
            candidates=[HatCandidate(**c) for c in candidates],
            intent=intent,
        )

        return response.model_dump()

    except Exception as e:
        print(f"HackSearch error: {e}")
        return {'statusCode': 500, 'error': str(e)}
