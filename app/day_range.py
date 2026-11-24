"""Persist an exam's vocabulary scope independently of its public Day/version key."""


def bounds(day, day_end=None):
    end = day if day_end is None else day_end
    if type(day) is not int or type(end) is not int or not 1 <= day <= end <= 365:
        raise ValueError("시작 Day와 끝 Day를 올바르게 선택해 주세요.")
    return day, end


def label(day, day_end=None):
    start, end = bounds(day, day_end)
    return f"DAY {start}" if start == end else f"DAY {start}~{end}"


def metadata(day, day_end=None):
    start, end = bounds(day, day_end)
    return {"day_start": start, "day_end": end}


def stored(ctx, question=None):
    """Legacy rows use their folder's scope; never infer a range from editable titles."""
    q = question or {}
    start = q.get("day_start") or ctx["day"]
    end = q.get("day_end") or ctx.get("day_end") or start
    values = metadata(int(start), int(end))
    if values["day_start"] != ctx["day"]:
        raise ValueError("시험지의 저장된 Day 범위를 확인해 주세요.")
    return values
