"""Shared per-speaker grouping for timestamped transcription word events."""


def group_utterances(events: list[dict], *, gap: float, stitch: float) -> list[dict]:
    """Keep each speaker's overlapping speech whole, retaining time ranges."""
    by_group: dict = {}
    for event in events:
        if str(event.get("text", "")).strip() and event.get("group") is not None:
            by_group.setdefault(event["group"], []).append(event)
    utterances = []
    for group, words in by_group.items():
        current = None
        for word in sorted(words, key=lambda item: item["start"]):
            if current is not None and word["start"] - current["end"] <= gap:
                current["text"] += " " + word["text"].strip()
                current["end"] = max(current["end"], word["end"])
            else:
                current = {
                    "group": group,
                    "start": word["start"],
                    "end": word["end"],
                    "text": word["text"].strip(),
                }
                utterances.append(current)
    merged = []
    for utterance in sorted(
        utterances, key=lambda item: (item["start"], str(item["group"]))
    ):
        if (
            merged
            and merged[-1]["group"] == utterance["group"]
            and utterance["start"] - merged[-1]["end"] < stitch
        ):
            merged[-1]["text"] += " " + utterance["text"]
            merged[-1]["end"] = max(merged[-1]["end"], utterance["end"])
        else:
            merged.append(dict(utterance))
    return merged
