"""Provenance-only labels and readable dark-theme source colours."""
from urllib.parse import urlsplit

SOURCE_STYLES = {
    "ЛУН": ("#c9a0ff", "#241e35"),
    "List.in.ua": ("#70d1e6", "#15303c"),
    "NOVOBUDOVY": ("#ffb678", "#352819"),
    "Prozorro": ("#e3c86d", "#322d1b"),
    "OSM": ("#80d7a0", "#183029"),
    "Сайт": ("#98b9ff", "#1b2b44"),
    "Кілька джерел": ("#c2b4e5", "#28253c"),
    "Без джерела": ("#b9c4d1", "#1b2637"),
}


def source_label(url: str) -> str:
    """Never infer provenance from a company's name or unrelated website."""
    raw = (url or "").strip()
    if not raw:
        return "Без джерела"
    if raw.lower().startswith(("osm:", "openstreetmap:")):
        return "OSM"
    try:
        parsed = urlsplit(raw if "://" in raw else "https://" + raw)
        host = (parsed.hostname or "").lower().removeprefix("www.")
    except ValueError:
        return "Без джерела"
    if host == "lun.ua" or host.endswith(".lun.ua"):
        return "ЛУН"
    if host == "list.in.ua" or host.endswith(".list.in.ua"):
        return "List.in.ua"
    if host == "novobudovy.com" or host.endswith(".novobudovy.com"):
        return "NOVOBUDOVY"
    if host == "prozorro.gov.ua" or host.endswith(".prozorro.gov.ua"):
        return "Prozorro"
    if host in {"osm.org", "openstreetmap.org"} or host.endswith(".openstreetmap.org"):
        return "OSM"
    return "Сайт" if host and "." in host else "Без джерела"


def group_source_label(rows: list[dict]) -> str:
    labels = {str(row.get("source_label") or source_label(row.get("source_url", ""))) for row in rows}
    return next(iter(labels)) if len(labels) == 1 else ("Кілька джерел" if labels else "Без джерела")
