def log_error(tag: str, e: Exception) -> None:
    print(f"[{tag}] {type(e).__name__}: {e}")
