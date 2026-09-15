"""Port of the prototype buffer policy; does not generate or play audio."""

from math import floor


def score(value: float) -> float:
    return floor(max(0, min(1, value)) * 100 + 0.5) / 100


def decide_next_segment(*, buffer_seconds, estimated_generation_seconds,
                        oldest_request_age_seconds, pending_request_count,
                        buffer_target_seconds, response_window_seconds):
    estimated_generation_seconds = max(1, estimated_generation_seconds)
    risk = estimated_generation_seconds / max(1, buffer_seconds)
    deficit = max(0, buffer_target_seconds - buffer_seconds) / buffer_target_seconds
    pressure = min(1, oldest_request_age_seconds / response_window_seconds) if pending_request_count else 0
    if pending_request_count and pressure >= 0.85 and risk < 0.9:
        action = "serve_request"
        reason = "oldest request is close to the response target and buffer risk is acceptable"
        priority = pressure + 1 - risk
    elif buffer_seconds < estimated_generation_seconds or deficit > pressure:
        action = "build_buffer"
        reason = "continuity buffer needs protection before reacting to more requests"
        priority = deficit + risk
    elif pending_request_count:
        action = "serve_request"
        reason = "buffer is stable enough to respond to the request queue"
        priority = pressure + 0.5
    else:
        action = "idle"
        reason = "no pending requests and buffer is within the target range"
        priority = 0
    return {"action": action, "reason": reason, "metrics": {
        "priority": score(priority), "underrunRisk": score(risk),
        "bufferDeficit": score(deficit), "requestPressure": score(pressure),
    }}
