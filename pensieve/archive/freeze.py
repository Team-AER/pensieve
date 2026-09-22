"""Freeze a rendered DOM into a static offline copy.

Scripts, frames, event handlers and preload hints are removed; stylesheets are inlined as ``<style>``; images,
fonts and CSS backgrounds point at content-addressed archive assets (``/archive/a/<sha256>``) so a logo
shared by a hundred saved pages is stored once. The result renders with no network access at all: the page is
served with a CSP that allows nothing but those assets and inline styles.
"""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Iterable
from datetime import datetime
from urllib.parse import urldefrag, urljoin, urlsplit

from lxml import etree
from lxml import html as lxml_html

from pensieve.archive.render import Resource

ASSET_PREFIX = "/archive/a/"

_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+?)\1\s*\)""", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r"""@import\s+(?:url\(\s*)?['"]?([^'")\s;]+)['"]?\s*\)?([^;]*);""", re.IGNORECASE)
_STRIP_TAGS = (
    "script",
    "noscript",
    "template",
    "object",
    "embed",
    "applet",
    "frameset",
    "frame",
    "base",
    "portal",
)
_DROP_LINK_RELS = {
    "preload",
    "prefetch",
    "modulepreload",
    "preconnect",
    "dns-prefetch",
    "manifest",
    "prerender",
    "icon",
    "shortcut",
    "apple-touch-icon",
    "mask-icon",
    "alternate",
    "canonical",
    "amphtml",
}
_URL_ATTRS = ("href", "src", "action", "formaction", "poster", "cite", "background")
IMAGE_TYPES = ("image/",)
FONT_TYPES = ("font/", "application/font", "application/x-font", "application/vnd.ms-fontobject")


def absolute(url: str | None, base: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url or url.startswith(("data:", "javascript:", "blob:", "about:", "#", "mailto:", "tel:")):
        return None
    try:
        joined = urldefrag(urljoin(base, url))[0]
    except ValueError:
        return None
    return joined if urlsplit(joined).scheme in {"http", "https"} else None


def _srcset_best(value: str | None, base: str) -> str | None:
    """Largest candidate of a srcset (by w or x descriptor)."""
    best, best_score = None, -1.0
    for part in (value or "").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        score = 1.0
        if len(bits) > 1:
            d = bits[1].lower()
            try:
                score = float(d[:-1]) if d[-1] in "wx" else 1.0
            except ValueError:
                score = 1.0
        if score > best_score:
            best, best_score = absolute(bits[0], base), score
    return best


def css_refs(css: str, base: str) -> tuple[list[str], list[str]]:
    """(url() references, @import targets) of a stylesheet, absolute."""
    imports = [u for u in (absolute(m.group(1), base) for m in _CSS_IMPORT_RE.finditer(css)) if u]
    urls = [
        u
        for u in (absolute(m.group(2), base) for m in _CSS_URL_RE.finditer(_CSS_IMPORT_RE.sub("", css)))
        if u
    ]
    return urls, imports


def decode_css(resource: Resource) -> str:
    try:
        return resource.body.decode("utf-8")
    except UnicodeDecodeError:
        return resource.body.decode("latin-1", errors="replace")


def _parse(dom_html: str):
    parser = lxml_html.HTMLParser(remove_comments=True, recover=True)
    return lxml_html.document_fromstring(dom_html.encode("utf-8", errors="replace"), parser=parser)


def collect_refs(dom_html: str, base: str, resources: dict[str, Resource]) -> list[str]:
    """URLs the frozen copy wants (images, stylesheets, their assets) that the browser did not capture."""
    try:
        tree = _parse(dom_html)
    except (ValueError, etree.ParserError):
        return []
    wanted: list[str] = []
    stylesheets: list[str] = []
    for img in tree.iter("img"):
        wanted.append(absolute(img.get("src"), base) or _srcset_best(img.get("srcset"), base) or "")
    for el in tree.xpath("//video[@poster]|//input[@type='image']"):
        wanted.append(absolute(el.get("poster") or el.get("src"), base) or "")
    for link in tree.iter("link"):
        if "stylesheet" in (link.get("rel") or "").lower().split():
            stylesheets.append(absolute(link.get("href"), base) or "")
    for style in tree.iter("style"):
        urls, imports = css_refs(style.text or "", base)
        wanted += urls
        stylesheets += imports
    for el in tree.xpath("//*[@style]"):
        wanted += css_refs(el.get("style") or "", base)[0]
    seen_css: set[str] = set()
    while stylesheets:
        sheet = stylesheets.pop()
        if not sheet or sheet in seen_css:
            continue
        seen_css.add(sheet)
        wanted.append(sheet)
        res = resources.get(sheet)
        if res is not None:
            urls, imports = css_refs(decode_css(res), sheet)
            wanted += urls
            stylesheets += imports
    out: list[str] = []
    for url in wanted:
        if url and url not in resources and url not in out:
            out.append(url)
    return out


def _is_asset(resource: Resource) -> bool:
    ct = resource.content_type
    return (
        ct.startswith(IMAGE_TYPES)
        or ct.startswith(FONT_TYPES)
        or ct in {"application/octet-stream", "binary/octet-stream"}
    )


class Freezer:
    """One freeze pass; ``used`` collects every resource the output references (they become archive assets)."""

    def __init__(self, resources: dict[str, Resource]) -> None:
        self.resources = resources
        self.used: dict[str, Resource] = {}

    def asset_url(self, url: str | None) -> str | None:
        if not url:
            return None
        res = self.resources.get(url)
        if res is None or not _is_asset(res):
            return None
        self.used[res.sha256] = res
        return ASSET_PREFIX + res.sha256

    def rewrite_css(self, css: str, base: str, depth: int = 0) -> str:
        # Inline @imports last: their text is already rewritten against *their* URL and must not be re-resolved.
        imports: list[str] = []

        def _import(m: re.Match) -> str:
            target = absolute(m.group(1), base)
            res = self.resources.get(target or "")
            if res is None or depth > 3:
                return ""
            media = m.group(2).strip()
            inner = self.rewrite_css(decode_css(res), target, depth + 1)
            imports.append(f"@media {media} {{\n{inner}\n}}\n" if media else inner + "\n")
            return f"/*pensieve-import-{len(imports) - 1}*/"

        css = _CSS_IMPORT_RE.sub(_import, css)

        def _url(m: re.Match) -> str:
            raw = m.group(2)
            if raw.startswith(
                ("data:", ASSET_PREFIX)
            ):  # already local (a <link> turned <style>, or inline data)
                return m.group(0)
            local = self.asset_url(absolute(raw, base))
            return f'url("{local}")' if local else "url()"

        css = _CSS_URL_RE.sub(_url, css)
        for n, block in enumerate(imports):
            css = css.replace(f"/*pensieve-import-{n}*/", block, 1)
        return css

    def freeze(self, dom_html: str, base: str, *, original_url: str, captured_at: datetime) -> str:
        tree = _parse(dom_html)
        for bad in tree.xpath("|".join(f"//{t}" for t in _STRIP_TAGS)):
            bad.drop_tree()
        for meta in tree.xpath("//meta[@http-equiv]"):
            meta.drop_tree()
        for frame in tree.xpath("//iframe"):
            src = absolute(frame.get("src"), base)
            if src:
                note = lxml_html.fragment_fromstring(
                    f'<p class="pensieve-embed"><a href="{html_lib.escape(src)}">Embedded content: '
                    f"{html_lib.escape(urlsplit(src).hostname or src)}</a></p>"
                )
                frame.addprevious(note)
            frame.drop_tree()
        for link in list(tree.iter("link")):
            rels = set((link.get("rel") or "").lower().split())
            if "stylesheet" in rels:
                href = absolute(link.get("href"), base)
                res = self.resources.get(href or "")
                if res is None:
                    link.drop_tree()
                    continue
                style = etree.Element("style")
                if link.get("media"):
                    style.set("media", link.get("media"))
                style.text = self.rewrite_css(decode_css(res), href)
                link.addprevious(style)
                link.drop_tree()
            elif rels & _DROP_LINK_RELS or not rels:
                link.drop_tree()
        for style in tree.iter("style"):
            if style.text:
                style.text = self.rewrite_css(style.text, base)
        for el in tree.iter(etree.Element):
            for attr in list(el.attrib):
                lower = attr.lower()
                if lower.startswith("on") or lower in {
                    "srcset",
                    "sizes",
                    "integrity",
                    "nonce",
                    "ping",
                    "autofocus",
                }:
                    del el.attrib[attr]
            if "style" in el.attrib:
                el.set("style", self.rewrite_css(el.get("style"), base))
            for attr in _URL_ATTRS:
                value = el.get(attr)
                if value is None:
                    continue
                if value.strip().lower().startswith(("javascript:", "vbscript:")):
                    el.set(attr, "#")
        for img in tree.iter("img"):
            local = self.asset_url(absolute(img.get("src"), base))
            if local:
                img.set("src", local)
            elif img.get("src") and not img.get("src", "").startswith("data:"):
                img.set("data-original-src", absolute(img.get("src"), base) or "")
                img.set("src", "data:,")
        for el in tree.xpath("//video[@poster]"):
            local = self.asset_url(absolute(el.get("poster"), base))
            el.set("poster", local or "")
        for el in tree.xpath("//video|//audio|//source|//track"):
            el.attrib.pop("src", None)
        for a in tree.iter("a"):
            href = absolute(a.get("href"), base)
            if href:
                a.set("href", href)
                a.set("target", "_blank")
                a.set("rel", "noopener noreferrer")
        for form in tree.iter("form"):
            form.attrib.pop("action", None)  # submitting an archived form must not post to the live site
        head = tree.find("head")
        if head is None:
            head = etree.SubElement(tree, "head")
            tree.insert(0, head)
        meta = etree.Element("meta")
        meta.set("charset", "utf-8")
        head.insert(0, meta)
        body = tree.find("body")
        if body is not None:
            body.insert(0, _banner(original_url, captured_at))
        out = lxml_html.tostring(tree, encoding="unicode", method="html", doctype="<!DOCTYPE html>")
        return out

    def rewrite_article(self, article_html: str, base: str) -> str:
        """Point a sanitised article's images at archive assets (and drop their remote srcsets)."""
        if not article_html:
            return article_html
        try:
            fragments = lxml_html.fragments_fromstring(article_html)
        except (ValueError, etree.ParserError):
            return article_html
        parts: list[str] = []
        for frag in fragments:
            if isinstance(frag, str):
                parts.append(html_lib.escape(frag))
                continue
            for img in frag.iter("img") if frag.tag != "img" else [frag]:
                src = absolute(img.get("src"), base) or _srcset_best(img.get("srcset"), base)
                local = self.asset_url(src)
                if local:
                    img.set("src", local)
                    img.attrib.pop("srcset", None)
                    img.attrib.pop("sizes", None)
            parts.append(lxml_html.tostring(frag, encoding="unicode", method="html"))
        return "".join(parts)


def _banner(original_url: str, captured_at: datetime):
    safe_url = html_lib.escape(original_url)
    host = html_lib.escape(urlsplit(original_url).hostname or original_url)
    when = captured_at.strftime("%-d %b %Y, %H:%M UTC")
    return lxml_html.fragment_fromstring(
        '<div id="pensieve-archive-banner" style="all:initial;display:block;box-sizing:border-box;width:100%;'
        "padding:7px 14px;background:#1B1A17;color:#F4F1EA;font:500 12.5px/1.45 system-ui,-apple-system,"
        'sans-serif;letter-spacing:.01em">'
        f'Archived copy of <a href="{safe_url}" target="_blank" rel="noopener noreferrer" '
        f'style="all:unset;color:#9CC7DE;text-decoration:underline;cursor:pointer">{host}</a>'
        f" &middot; captured {when} by Pensieve</div>"
    )


def pick_urls(urls: Iterable[str], limit: int) -> list[str]:
    """Deduplicated first ``limit`` urls (keeps page order: above-the-fold images first)."""
    out: list[str] = []
    for url in urls:
        if url and url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


__all__ = ["ASSET_PREFIX", "Freezer", "absolute", "collect_refs", "css_refs", "pick_urls"]
