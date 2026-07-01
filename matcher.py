"""SKU matching — decide whether a competitor product is actually yours.

This is a big part of turning a price feed into signal instead of noise: a competitor's
"Logitech Wireless Mouse" is not necessarily your SKU. Strategy:
  1. Exact match on GTIN/UPC/EAN if both sides have one. ScrapingBee's Walmart parser
     returns `gtin`; Amazon's `product_details` often carries a UPC/EAN — use them.
  2. Else compare model codes (M185 != M510) — the real discriminator for same-brand items.
  3. Else fuzzy-match normalized titles and return a confidence score so you can
     auto-accept high-confidence pairs and queue the rest for human review.

Standard library only (difflib). Swap in rapidfuzz if you need speed at scale.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

_STOP = {"the", "with", "for", "and", "by", "of", "new", "renewed", "pack", "color", "size"}
_MODEL = re.compile(r"\b[a-z]{1,4}\d{2,5}[a-z]?\b")   # model codes like m185, mx518 (not "4ghz" or "1000")


def normalize_title(title: str) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", title.lower())     # strip punctuation
    return " ".join(w for w in t.split() if w not in _STOP)


def model_codes(title: str) -> set[str]:
    return set(_MODEL.findall(re.sub(r"[^a-z0-9 ]+", " ", title.lower())))


@dataclass
class MatchResult:
    matched: bool
    confidence: float          # 0.0–1.0
    method: str                # "gtin" | "model" | "title"
    needs_review: bool


def match(our: dict, comp: dict, *, accept: float = 0.8, review: float = 0.6) -> MatchResult:
    """`our` and `comp` are dicts with at least 'title'; 'gtin' optional on either side."""
    # 1. GTIN/UPC is the ground truth when both sides have it
    our_gtin, comp_gtin = (our.get("gtin") or "").strip(), (comp.get("gtin") or "").strip()
    if our_gtin and comp_gtin:
        same = our_gtin == comp_gtin
        return MatchResult(same, 1.0 if same else 0.0, "gtin", needs_review=False)

    # 2. model codes are the real discriminator (M185 != M510, even though titles look alike)
    a, b = model_codes(our["title"]), model_codes(comp["title"])
    if a and b:
        if a & b:
            return MatchResult(True, 0.95, "model", needs_review=False)
        return MatchResult(False, 0.1, "model", needs_review=False)   # different models = different products

    # 3. fall back to fuzzy title, blending token overlap with sequence similarity
    na, nb = normalize_title(our["title"]), normalize_title(comp["title"])
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    score = round(0.5 * jaccard + 0.5 * SequenceMatcher(None, na, nb).ratio(), 2)
    if score >= accept:
        return MatchResult(True, score, "title", needs_review=False)
    if score >= review:
        return MatchResult(True, score, "title", needs_review=True)
    return MatchResult(False, score, "title", needs_review=False)


if __name__ == "__main__":
    our = {"title": "Logitech M185 Wireless Mouse, Swift Grey", "gtin": ""}
    candidates = [
        {"title": "Logitech M185 Wireless Mouse, 2.4GHz with USB Mini Receiver - Swift Grey"},
        {"title": "Logitech Silent Wireless Mouse, Blue/Gray, Walmart Exclusive"},
        {"title": "Logitech M510 Wireless Mouse, 2.4 GHz USB Unifying Receiver"},
    ]
    for c in candidates:
        r = match(our, c)
        tag = "MATCH" if r.matched and not r.needs_review else ("REVIEW" if r.needs_review else "no match")
        print(f"  [{tag:8}] conf={r.confidence:<4} via {r.method:5} :: {c['title'][:55]}")
