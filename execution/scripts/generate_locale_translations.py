#!/usr/bin/env python3
"""Generate machine-translated locale assets for new website languages."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from deep_translator import GoogleTranslator

ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = ROOT / "execution" / "website" / "markmhendrickson" / "react-app"
CACHE_EN_PATH = APP_ROOT / "cache" / "posts.en.json"
TIMELINE_CACHE_PATH = APP_ROOT / "cache" / "timeline.json"
LINKS_CACHE_PATH = APP_ROOT / "cache" / "links.json"
POST_TRANSLATIONS_DIR = APP_ROOT / "src" / "content" / "posts"
DICT_TS_PATH = APP_ROOT / "src" / "i18n" / "dictionaries.ts"
DICT_GEN_PATH = APP_ROOT / "src" / "i18n" / "dictionaries.generated.json"
CONTENT_GEN_PATH = APP_ROOT / "src" / "i18n" / "content.generated.json"
AGENT_JSON_PATH = APP_ROOT / "public" / "api" / "agent.json"
GLOSSARY_PATH = POST_TRANSLATIONS_DIR / "translation_glossary.json"

LOCALE_TO_TRANSLATOR_LANG = {
    "es": "es",
    "ca": "ca",
    "zh": "zh-CN",
    "hi": "hi",
    "ar": "ar",
    "fr": "fr",
    "pt": "pt",
    "ru": "ru",
    "bn": "bn",
    "ur": "ur",
    "id": "id",
    "de": "de",
}


# ---------------------------------------------------------------------------
# Glossary: context-aware disambiguation for polysemous terms
# ---------------------------------------------------------------------------

_glossary_cache: dict | None = None


def _load_glossary() -> dict:
    """Load the translation glossary (heading overrides + forbidden senses)."""
    global _glossary_cache
    if _glossary_cache is not None:
        return _glossary_cache
    if not GLOSSARY_PATH.exists():
        _glossary_cache = {}
        return _glossary_cache
    try:
        _glossary_cache = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: Failed to load glossary {GLOSSARY_PATH}: {exc}")
        _glossary_cache = {}
    return _glossary_cache


def _apply_heading_overrides(text: str, locale: str) -> str:
    """Replace glossary-matched phrases inside markdown headings before MT.

    Scans for ## or ### headings and substitutes known phrases with their
    canonical locale translation so the MT engine never sees the ambiguous
    English term in isolation.
    """
    glossary = _load_glossary()
    overrides = glossary.get("heading_overrides", {})
    if not overrides:
        return text

    lines = text.split("\n")
    result: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            heading_text = stripped.lstrip("#").strip()
            heading_lower = heading_text.lower()
            for en_phrase, locale_map in overrides.items():
                if en_phrase.lower() in heading_lower and locale in locale_map:
                    pattern = re.compile(re.escape(en_phrase), re.IGNORECASE)
                    line = pattern.sub(locale_map[locale], line)
        result.append(line)
    return "\n".join(result)


def _validate_forbidden_senses(
    translated: str, locale: str, context: str = ""
) -> list[str]:
    """Check translated text for known wrong-sense translations (e.g. 'tardor' for 'the fall').

    Returns a list of warning strings for each violation found.
    """
    glossary = _load_glossary()
    forbidden = glossary.get("forbidden_senses", {})
    warnings: list[str] = []
    for en_term, locale_map in forbidden.items():
        if not isinstance(locale_map, dict) or locale not in locale_map:
            continue
        bad_list = sorted(locale_map[locale], key=len, reverse=True)
        for bad_translation in bad_list:
            pattern = re.compile(
                r"\b" + re.escape(bad_translation) + r"\b", re.IGNORECASE
            )
            if pattern.search(translated):
                ctx = f" (in: {context})" if context else ""
                warnings.append(
                    f"[{locale}] Possible wrong-sense translation for '{en_term}': "
                    f"found '{bad_translation}' in heading{ctx}"
                )
    return warnings


def _chunk_text(text: str, max_chars: int = 4200) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current = []
    current_len = 0
    for para in text.split("\n\n"):
        needed = len(para) + (2 if current else 0)
        if current and current_len + needed > max_chars:
            parts.append("\n\n".join(current))
            current = [para]
            current_len = len(para)
        else:
            current.append(para)
            current_len += needed
    if current:
        parts.append("\n\n".join(current))
    return parts


def _translate_text(
    text: str,
    translator: GoogleTranslator,
    cache: dict[str, str],
    locale: str = "",
    apply_glossary: bool = False,
) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    cache_key = raw
    if cache_key in cache:
        return cache[cache_key]

    source = raw
    if apply_glossary and locale:
        source = _apply_heading_overrides(source, locale)

    chunks = _chunk_text(source)
    translated_chunks = []
    for chunk in chunks:
        try:
            translated_chunks.append(translator.translate(chunk) or chunk)
        except Exception:
            translated_chunks.append(chunk)
    translated = "\n\n".join(translated_chunks)
    cache[cache_key] = translated
    return translated


def _extract_en_dict() -> dict[str, str]:
    content = DICT_TS_PATH.read_text(encoding="utf-8")
    marker = "const enDict: Dict = {"
    start = content.index(marker) + len(marker)
    end = content.index("\nconst esDict: Dict = {", start)
    block = content[start:end]

    parsed: dict[str, str] = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("}"):
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip().rstrip(",")
        if value.startswith("'") and value.endswith("'"):
            parsed[key] = value[1:-1]
        elif value.startswith('"') and value.endswith('"'):
            parsed[key] = value[1:-1]
    return parsed


def _translate_dicts() -> None:
    en_dict = _extract_en_dict()
    generated: dict[str, dict[str, str]] = {}
    for locale, target_lang in LOCALE_TO_TRANSLATOR_LANG.items():
        translator = GoogleTranslator(source="en", target=target_lang)
        cache: dict[str, str] = {}
        translated: dict[str, str] = {}
        for key, text in en_dict.items():
            translated[key] = _translate_text(text, translator, cache, locale=locale)
        generated[locale] = translated
        print(f"Translated UI dictionary for {locale}: {len(translated)} keys")
    DICT_GEN_PATH.write_text(
        json.dumps(generated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {DICT_GEN_PATH}")


def _translate_posts() -> None:
    posts = json.loads(CACHE_EN_PATH.read_text(encoding="utf-8"))
    if not isinstance(posts, list):
        raise RuntimeError(f"Expected list in {CACHE_EN_PATH}")
    all_warnings: list[str] = []
    for locale, target_lang in LOCALE_TO_TRANSLATOR_LANG.items():
        path = POST_TRANSLATIONS_DIR / f"translations.{locale}.json"
        existing = {}
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        translator = GoogleTranslator(source="en", target=target_lang)
        cache: dict[str, str] = {}
        output: dict[str, dict[str, Any]] = {}
        for post in posts:
            if not isinstance(post, dict):
                continue
            slug = post.get("canonicalSlug") or post.get("postId") or post.get("slug")
            if not slug:
                continue
            prior = (
                existing.get(slug, {}) if isinstance(existing.get(slug), dict) else {}
            )
            title = prior.get("title") or _translate_text(
                str(post.get("title") or ""), translator, cache, locale=locale
            )
            excerpt = prior.get("excerpt") or _translate_text(
                str(post.get("excerpt") or ""), translator, cache, locale=locale
            )
            summary = prior.get("summary") or _translate_text(
                str(post.get("summary") or ""), translator, cache, locale=locale
            )
            body = prior.get("body") or _translate_text(
                str(post.get("body") or ""),
                translator,
                cache,
                locale=locale,
                apply_glossary=True,
            )
            source_postscript = ""
            postscript_path = POST_TRANSLATIONS_DIR / f"{slug}.postscript.md"
            if not postscript_path.exists():
                postscript_path = POST_TRANSLATIONS_DIR / "drafts" / f"{slug}.postscript.md"
            if postscript_path.exists():
                source_postscript = postscript_path.read_text(encoding="utf-8").strip()
            entry: dict[str, Any] = {
                "title": title,
                "excerpt": excerpt,
                "summary": summary,
                "body": body,
            }
            if source_postscript:
                entry["postscript"] = prior.get("postscript") or _translate_text(
                    source_postscript, translator, cache
                )
            if post.get("shareDescription"):
                entry["shareDescription"] = prior.get(
                    "shareDescription"
                ) or _translate_text(
                    str(post.get("shareDescription")), translator, cache, locale=locale
                )
            if prior.get("slug"):
                entry["slug"] = prior.get("slug")
            if prior.get("alternativeSlugs"):
                entry["alternativeSlugs"] = prior.get("alternativeSlugs")

            for field_name in ("title", "body"):
                field_val = entry.get(field_name, "")
                if not field_val:
                    continue
                heading_lines = [
                    ln for ln in field_val.split("\n") if ln.lstrip().startswith("#")
                ]
                for hl in heading_lines:
                    ws = _validate_forbidden_senses(
                        hl, locale, context=f"{slug}/{field_name}"
                    )
                    all_warnings.extend(ws)

            output[slug] = entry
        path.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Wrote {path} ({len(output)} posts)")
    if all_warnings:
        print(f"\nGlossary validation warnings ({len(all_warnings)}):")
        for w in all_warnings:
            print(f"  WARNING: {w}")


def _translate_agent_json() -> None:
    source = json.loads(AGENT_JSON_PATH.read_text(encoding="utf-8"))

    def recurse_translate(
        value: Any, translator: GoogleTranslator, cache: dict[str, str], locale: str
    ) -> Any:
        if isinstance(value, str):
            if value.startswith("http://") or value.startswith("https://"):
                return value
            return _translate_text(value, translator, cache, locale=locale)
        if isinstance(value, list):
            return [recurse_translate(v, translator, cache, locale) for v in value]
        if isinstance(value, dict):
            out = {}
            for key, val in value.items():
                if key in {"url", "mcpServerRepoUrl"}:
                    out[key] = val
                else:
                    out[key] = recurse_translate(val, translator, cache, locale)
            return out
        return value

    for locale, target_lang in LOCALE_TO_TRANSLATOR_LANG.items():
        translator = GoogleTranslator(source="en", target=target_lang)
        cache: dict[str, str] = {}
        translated = recurse_translate(source, translator, cache, locale)
        translated["url"] = f"https://markmhendrickson.com/api/agent.{locale}.json"
        path = APP_ROOT / "public" / "api" / f"agent.{locale}.json"
        path.write_text(
            json.dumps(translated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {path}")


def _translate_content_maps() -> None:
    timeline = json.loads(TIMELINE_CACHE_PATH.read_text(encoding="utf-8"))
    links = json.loads(LINKS_CACHE_PATH.read_text(encoding="utf-8"))
    if not isinstance(timeline, list):
        raise RuntimeError(f"Expected list in {TIMELINE_CACHE_PATH}")
    if not isinstance(links, list):
        raise RuntimeError(f"Expected list in {LINKS_CACHE_PATH}")

    timeline_roles: list[str] = []
    timeline_dates: list[str] = []
    timeline_desc: list[str] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        date = str(item.get("date") or "").strip()
        if role and role not in timeline_roles:
            timeline_roles.append(role)
        if date and date not in timeline_dates:
            timeline_dates.append(date)
        for line in item.get("description") or []:
            desc = str(line or "").strip()
            if desc and desc not in timeline_desc:
                timeline_desc.append(desc)

    link_descriptions: list[str] = []
    for item in links:
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description") or "").strip()
        if desc and desc not in link_descriptions:
            link_descriptions.append(desc)

    schedule_source = {
        "title": "Meet with me",
        "subtitle": "Pick a time that works for you",
        "pageDesc": "Book a 30 or 60 minute slot with Mark.",
        "bookVia": "Book via Notion Calendar",
        "duration30": "30 minutes",
        "duration60": "60 minutes",
        "labelMeeting": "Meeting",
        "labelChat": "Chat",
    }

    generated: dict[str, Any] = {}
    for locale, target_lang in LOCALE_TO_TRANSLATOR_LANG.items():
        translator = GoogleTranslator(source="en", target=target_lang)
        cache: dict[str, str] = {}
        generated[locale] = {
            "timeline": {
                "roles": {
                    text: _translate_text(text, translator, cache, locale=locale)
                    for text in timeline_roles
                },
                "dates": {
                    text: _translate_text(text, translator, cache, locale=locale)
                    for text in timeline_dates
                },
                "descriptions": {
                    text: _translate_text(text, translator, cache, locale=locale)
                    for text in timeline_desc
                },
            },
            "links": {
                "descriptions": {
                    text: _translate_text(text, translator, cache, locale=locale)
                    for text in link_descriptions
                },
            },
            "schedule": {
                key: _translate_text(text, translator, cache, locale=locale)
                for key, text in schedule_source.items()
            },
        }
        print(
            f"Translated content maps for {locale}: "
            f"{len(timeline_roles)} roles, {len(timeline_dates)} dates, "
            f"{len(timeline_desc)} timeline lines, {len(link_descriptions)} link lines"
        )

    CONTENT_GEN_PATH.write_text(
        json.dumps(generated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {CONTENT_GEN_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate machine-translated locale assets for website locales."
    )
    parser.add_argument(
        "--posts-only",
        action="store_true",
        help="Translate post content maps only (skip dictionaries, agent JSON, and content maps).",
    )
    args = parser.parse_args()

    if args.posts_only:
        _translate_posts()
        return

    _translate_dicts()
    _translate_posts()
    _translate_agent_json()
    _translate_content_maps()


if __name__ == "__main__":
    main()
