"""Product image discovery, proxying, and disk caching.

Extracted from the web app so the routes stay thin and the warmup job can
run from the CLI monitor process instead of a thread inside the Flask
factory.  All outbound fetches go through ``http_safety.safe_get`` so
user-supplied URLs (and their redirect chains) can't reach internal hosts.
"""
import hashlib
import html as _html_mod
import json as _json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

from sale_monitor.services.http_safety import is_public_url, safe_get
from sale_monitor.storage.file_lock import FileLock

logger = logging.getLogger(__name__)

# Set Pillow decompression bomb limit
try:
    from PIL import Image as _PILImage
    _PILImage.MAX_IMAGE_PIXELS = 25_000_000  # ~5000x5000
except ImportError:
    pass


def extract_image_url(html: str, base_url: str) -> Optional[str]:
    """Extract representative product image URL from HTML.
    Preference order:
    1) Amazon-specific image extraction
    2) JSON-LD Product image
    3) OpenGraph/Twitter card image
    4) <img> with explicit product-ish classes/attributes (data-zoom-image, srcset largest)
    """

    def _abs(u: str) -> str:
        if not u:
            return u
        p = urlparse(u)
        if not p.scheme:
            if u.startswith('//'):
                base_p = urlparse(base_url)
                scheme = base_p.scheme or 'https'
                return f"{scheme}:{u}"
            return urljoin(base_url, u)
        # Prefer https if base is https
        base_p = urlparse(base_url)
        if p.scheme == 'http' and (base_p.scheme or '').lower() == 'https':
            return u.replace('http://', 'https://', 1)
        return u

    if not html:
        return None

    # 1) Amazon-specific image extraction
    if 'amazon.' in base_url.lower():
        # Amazon product images - multiple methods
        amazon_patterns = [
            # Main product image (landingImage data)
            r'"colorImages":\s*{\s*"initial":\s*\[\s*{\s*"large":\s*"([^"]+)"',
            r'"landingImage":\s*"([^"]+)"',
            # Image gallery
            r'"hiRes":\s*"([^"]+)"',
            r'"large":\s*"([^"]+)"',
            # Fallback to img tag with specific ID
            r'<img[^>]+id=["\']landingImage["\'][^>]*src=["\']([^"\']+)["\']',
            r'<img[^>]+data-old-hires=["\']([^"\']+)["\']',
            r'<img[^>]+data-a-dynamic-image=["\'][{][^}]*["\']([^"\']+)["\']',
        ]
        for pat in amazon_patterns:
            m = re.search(pat, html, re.I)
            if m:
                img_url = m.group(1).strip()
                # Clean up Amazon image URLs (remove size constraints for better quality)
                img_url = re.sub(r'\._[A-Z0-9,_]+_\.', '.', img_url)
                return _abs(img_url)

    # 2) JSON-LD Product image
    def _find_image(obj):
        if isinstance(obj, dict):
            for k in ('image', 'imageUrl', 'thumbnailUrl'):
                if k in obj:
                    v = obj[k]
                    if isinstance(v, str):
                        return v
                    if isinstance(v, list) and v and isinstance(v[0], str):
                        # choose first
                        return v[0]
            for v in obj.values():
                out = _find_image(v)
                if out:
                    return out
        elif isinstance(obj, list):
            for it in obj:
                out = _find_image(it)
                if out:
                    return out
        return None

    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
        block = m.group(1).strip()
        try:
            data = _json.loads(block)
        except (_json.JSONDecodeError, ValueError, TypeError):
            continue
        img = _find_image(data)
        if img:
            return _abs(img.strip())

    # 2) OG/Twitter meta
    meta_patterns = (
        r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']twitter:image(?:\:src)?["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:image(?:\:src)?["\'][^>]*content=["\']([^"\']+)["\']',
    )
    for pat in meta_patterns:
        m = re.search(pat, html, re.I)
        if m:
            return _abs(m.group(1).strip())

    # 3) <img> with product-ish cues, prefer zoom/large attributes
    # 3a) data-zoom-image / data-large_image / data-src
    attr_patterns = (
        r'<img[^>]+data-zoom-image=["\']([^"\']+)["\']',
        r'<img[^>]+data-large[_-]image=["\']([^"\']+)["\']',
        r'<img[^>]+data-src=["\']([^"\']+)["\']',
    )
    for pat in attr_patterns:
        m = re.search(pat, html, re.I)
        if m:
            return _abs(m.group(1).strip())

    # 3b) srcset: choose the largest width
    m = re.search(r'<img[^>]+srcset=["\']([^"\']+)["\'][^>]*', html, re.I)
    if m:
        srcset = m.group(1)
        # parse entries: url [Nw]
        candidates = []
        for part in srcset.split(','):
            seg = part.strip()
            if not seg:
                continue
            pieces = seg.split()
            url = pieces[0]
            w = 0
            if len(pieces) > 1 and pieces[1].endswith('w'):
                try:
                    w = int(pieces[1][:-1])
                except ValueError:
                    w = 0
            candidates.append((w, url))
        if candidates:
            candidates.sort()
            return _abs(candidates[-1][1].strip())

    # 3c) generic product/main image class or alt pattern
    patterns = [
        r'<img[^>]+class=["\'][^"\']*(?:product|main|primary|image)[^"\']*["\'][^>]*src=["\']([^"\']+)["\']',
        r'<img[^>]+src=["\']([^"\']+)["\'][^>]*alt=["\'][^"\']*(?:product|frame|brake|derailleur|hoops|dream|machine|u7)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return _abs(m.group(1).strip())

    return None


class ImageService:
    """Fetches, resizes, and caches product images on disk."""

    _MEMORY_CACHE_MAX = 500

    def __init__(self, *, user_agent: str, timeout: int, data_dir: str):
        self.user_agent = user_agent
        self.timeout = timeout
        self.images_dir = Path(data_dir) / 'images'
        # In-memory image-URL cache: {product_url: {image_url, fetched}}
        self._url_cache: dict = {}

    # ── image URL discovery ──────────────────────────────────────────────

    def _url_cache_set(self, url: str, entry: dict):
        self._url_cache[url] = entry
        if len(self._url_cache) > self._MEMORY_CACHE_MAX:
            # Evict oldest entry
            oldest = min(
                self._url_cache,
                key=lambda k: self._url_cache[k].get(
                    'fetched', datetime.min.replace(tzinfo=timezone.utc)
                ),
            )
            del self._url_cache[oldest]

    def fetch_image_url(self, product_url: str) -> Optional[str]:
        """Find the representative image URL for a product page (24h cached)."""
        now = datetime.now(timezone.utc)
        cached = self._url_cache.get(product_url)
        if cached and (now - cached['fetched']) < timedelta(hours=24):
            return cached['image_url']
        try:
            headers = {
                'User-Agent': self.user_agent,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
            }
            resp = safe_get(product_url, headers=headers, timeout=self.timeout)
            if resp is None or resp.status_code >= 400:
                return None
            img = extract_image_url(resp.text, product_url)
            if img:
                img = _html_mod.unescape(img)
            if img and not is_public_url(img):
                return None
            if img:
                self._url_cache_set(product_url, {'image_url': img, 'fetched': now})
            return img
        except requests.exceptions.RequestException:
            return None

    # ── resized disk cache ───────────────────────────────────────────────

    def ensure_cached_file(self, product_url: str, max_w: int, max_h: int) -> Optional[Path]:
        """Ensure a resized, cached image exists for the given product URL.
        Returns Path to cached file or None on failure.
        """
        from PIL import Image, ImageOps

        image_url = self.fetch_image_url(product_url)
        if not image_url:
            return None
        key = hashlib.sha256(f"{image_url}|{max_w}x{max_h}".encode('utf-8')).hexdigest()
        guessed_ext = (Path(image_url).suffix or '').lower()
        ext = '.png' if guessed_ext in ('.png', '.webp') else '.jpg'

        self.images_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.images_dir / f"{key}{ext}"
        if out_path.exists():
            return out_path
        try:
            img_headers = {
                'User-Agent': self.user_agent,
                'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Sec-Fetch-Dest': 'image',
                'Sec-Fetch-Mode': 'no-cors',
                'Sec-Fetch-Site': 'cross-site',
            }
            r = safe_get(image_url, headers=img_headers, timeout=self.timeout, stream=True)
            if r is None:
                return None
            if r.status_code >= 400:
                # Retry without User-Agent — some CDNs block bot-like UAs for images
                img_headers.pop('User-Agent', None)
                r = safe_get(image_url, headers=img_headers, timeout=self.timeout, stream=True)
            if r is None or r.status_code >= 400:
                return None
            content_type = r.headers.get('Content-Type', '').lower()
            if not content_type.startswith('image/'):
                # Reject non-image content types — don't trust URL extension alone
                return None
            # Enforce a hard cap on downloaded bytes (e.g., 5 MiB)
            max_bytes = int(os.getenv('IMAGE_MAX_BYTES', '5242880'))
            read = 0
            buf = BytesIO()
            for chunk in r.iter_content(65536):
                if not chunk:
                    break
                read += len(chunk)
                if read > max_bytes:
                    return None
                buf.write(chunk)
            buf.seek(0)
            raw = buf
        except requests.exceptions.RequestException:
            return None
        try:
            with Image.open(raw) as im:
                im = ImageOps.exif_transpose(im)
                if im.mode in ('P', 'LA'):
                    im = im.convert('RGBA')
                im.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
                has_alpha = im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info)
                fmt = 'PNG' if has_alpha or ext == '.png' else 'JPEG'
                if fmt == 'JPEG' and im.mode not in ('RGB', 'L'):
                    im = im.convert('RGB')
                save_kwargs = {'quality': 85, 'optimize': True} if fmt == 'JPEG' else {}
                out_tmp = out_path.with_suffix(out_path.suffix + '.tmp')
                with open(out_tmp, 'wb') as f:
                    im.save(f, format=fmt, **save_kwargs)
                out_tmp.replace(out_path)
        except Exception:
            return None
        return out_path

    # ── warmup (run from the CLI monitor loop) ───────────────────────────

    def warmup_once(self, products) -> None:
        """Prefetch and cache images for enabled products.

        Throttled cross-process via a stamp file + file lock, so it is safe
        to call from multiple processes/containers sharing the data volume.
        """
        self.images_dir.mkdir(parents=True, exist_ok=True)
        stamp_path = self.images_dir / '.last_warmup'
        lock = FileLock(str(self.images_dir / 'warmup'))
        try:
            lock.acquire()
            now = datetime.now(timezone.utc)
            last = None
            try:
                if stamp_path.exists():
                    ts = stamp_path.read_text().strip()
                    last = datetime.fromisoformat(ts)
            except (OSError, ValueError):
                last = None
            interval_min = int(os.getenv('IMAGE_WARMUP_INTERVAL_MIN', '360'))
            if last and (now - last) < timedelta(minutes=interval_min):
                return
            max_w = int(os.getenv('IMAGE_CACHE_WIDTH', '600'))
            max_h = int(os.getenv('IMAGE_CACHE_HEIGHT', '220'))
            for p in products:
                try:
                    if not getattr(p, 'enabled', True):
                        continue
                    url = getattr(p, 'url', None)
                    if not url or not str(url).lower().startswith(('http://', 'https://')):
                        continue
                    self.ensure_cached_file(url, max_w, max_h)
                except Exception:
                    continue
            try:
                stamp_path.write_text(datetime.now(timezone.utc).isoformat())
            except OSError:
                pass
        finally:
            try:
                lock.release()
            except OSError:
                pass
