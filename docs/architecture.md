# Backend Architecture Notes

## Core loop

1. Listener requests arrive with a prompt and timestamp.
2. The scheduler estimates whether serving a request now risks stream underrun.
3. If the buffer is healthy, the scheduler prioritizes the oldest waiting request.
4. If the buffer is thin, the scheduler schedules continuity filler first.
5. Each decision is logged so the policy can later be evaluated against continuity and responsiveness metrics.

## Policy inputs

- `bufferSeconds`: playable audio already prepared for the shared stream
- `estimatedGenerationSeconds`: expected latency for the next generated segment
- `oldestRequestAgeSeconds`: how long the oldest listener request has waited
- `bufferTargetSeconds`: desired minimum comfort buffer
- `responseWindowSeconds`: target maximum request wait before the system should strongly favor responsiveness

## Policy outputs

- `serve_request`: generate or air a listener-requested segment
- `build_buffer`: create continuity material before serving more requests
- `idle`: no immediate work needed

This policy is intentionally small and replaceable. It gives the project a concrete backend surface while leaving room for experiments with adaptive scheduling, predictive latency, and fairness.
