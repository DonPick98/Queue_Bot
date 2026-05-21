RANDOM = "random"
CHRONOLOGICAL = "chronological"


def parse_queue_order(raw: str) -> str:
    value = raw.strip().lower()
    if value in {"random", "casuale", "randomico"}:
        return RANDOM
    if value in {"chronological", "chrono", "cronologico", "oldest", "vecchi"}:
        return CHRONOLOGICAL
    raise ValueError("Usa /set_queue_order random oppure /set_queue_order chronological.")
