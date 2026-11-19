"""onair-bank — 이야깃거리 데이터셋을 만들고, 벡터를 미리 채우고, 검색을 눈으로 확인한다 (#332).

    onair-bank build --llm gemini --llm-model gemini-3.8-flash   # 씨앗(themes.yaml)에서 items/를 만든다
    onair-bank index --embedder gemini                            # 벡터 캐시를 미리 채운다 (배포 뒤 한 번)
    onair-bank search "시험 기간을 버티는 나만의 방법" --embedder gemini --persona config/personas/saebyeok.yaml

데이터셋은 외부 글을 긁지 않고 LLM으로 만든다. 주제 × 페르소나(프리셋 + 공통) × 형식으로 묻고, 방송에 나갈
말의 재료이므로 대본과 같은 L2 안전 규칙을 통과한 것만 남긴다. 쓰는 쪽은 inputs/talk_bank.py다.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path

import yaml

from .domain import Prompt
from .inputs.talk_bank import (
    COMMON,
    ITEMS_DIR,
    PERSONAS_FILE,
    BankItem,
    TalkBank,
    load_items,
    load_personas,
    persona_text,
)
from .inputs.topic_plan import parse_angles
from .main import add_llm_args, load_env
from .pipeline.embed import make_embedder
from .pipeline.llm import LlmError, make_llm
from .pipeline.safety import SafetyChecker
from .prompt_data import DATA_RULES, STATION_CONCEPT, data_block

log = logging.getLogger(__name__)

SEED_FILE = "themes.yaml"
THEMES = "주제 목록"
MIN_POINTS = 3  # 말할 거리가 이보다 적은 갈래는 버린다 — 주제 토크 한 번을 채우지 못한다
MAX_TOKENS = 16000
LLM_TIMEOUT_SEC = 180.0  # 한 번에 갈래 수십 개를 받는다 — 대본용 기본값(20초)으로는 끊긴다
_SYSTEM = "너는 라디오 방송 구성 작가다. DJ가 방송에서 꺼낼 이야깃거리를 미리 만들어 둔다.\n" + DATA_RULES


def build_prompt(themes: list[str], per_theme: int, guide: str, persona: dict | None) -> Prompt:
    """persona는 프리셋 파일의 내용 — None이면 어느 방송에나 쓰는 공통 이야깃거리를 만든다."""
    lines = [
        f"이야깃거리 데이터셋: 아래 주제마다 서로 겹치지 않는 이야기 갈래 {per_theme}개를 만든다.",
        f"- {guide}",
        "- 갈래마다 짧은 제목 하나와 DJ가 말할 거리 4~6개를 붙인다.",
        "- 말할 거리는 순서대로 이어지게 쓴다. 앞의 것으로 이야기를 꺼내고, 가운데에서 풀어 가고, 마지막 것으로 마무리한다.",
        "- 말할 거리는 떠오르는 장면, 흔히 겪는 일, 둘 중 하나 고르기처럼 구체적으로 쓴다. 질문 문장으로 쓰지 않는다.",
        "- 수치, 가격, 상호, 날짜, 역사적 사실, 실제 인물과 작품 이름처럼 확인할 수 없는 사실은 넣지 않는다.",
        "- DJ가 직접 겪은 일처럼 쓰지 않는다.",
        "- 같은 주제의 갈래끼리는 장면과 결이 달라야 한다. 한 갈래를 말만 바꿔 되풀이하지 않는다.",
    ]
    blocks = [data_block(THEMES, "\n".join(f"- {t}" for t in themes))]
    if persona is None:
        lines.append("- 어느 방송에서나 쓸 수 있게 쓴다. 특정 시간대나 청취자의 처지에 기대지 않는다.")
    else:
        lines.append("- 아래 스테이션 컨셉의 청취자가 지금 겪고 있을 장면으로 쓴다. 듣는 시간대와 처지를 살린다. "
                     "DJ의 말투는 흉내 내지 않는다 — 말할 내용만 쓴다.")
        if persona.get("forbidden"):
            lines.append("- 다음은 넣지 않는다: " + "; ".join(persona["forbidden"]))
        about = [persona["concept"], persona.get("description", ""), persona["tone"]]
        blocks.append(data_block(STATION_CONCEPT, "\n".join(line for line in about if line)))
    lines += [
        ('- 출력은 JSON 하나만: {"themes": [{"theme": "주제 그대로", "angles": '
         '[{"title": "갈래 제목", "points": ["말할 거리", ...]}]}]}'),
        "", *blocks,
    ]
    return Prompt(system=_SYSTEM, user="\n".join(lines))


def item_id(fmt: str, persona: str, theme: str, title: str) -> str:
    return f"{fmt}.{persona}.{hashlib.sha1(f'{theme}|{title}'.encode()).hexdigest()[:8]}"


def parse_items(text: str, themes: list[str], fmt: str, persona: str, violates) -> list[BankItem]:
    """LLM 출력 → 이야깃거리. 형식이 틀리거나 말할 거리가 모자라거나 안전 규칙에 걸린 갈래는 버린다."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("JSON이 없습니다")
    data = json.loads(text[start:end + 1])
    groups = data.get("themes") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise ValueError("themes 배열이 없습니다")  # noqa: TRY004 — 호출자는 ValueError 하나로 받는다
    items = []
    for n, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get("angles"), list):
            continue
        # 주제 이름을 조금 바꿔 적어도 순서로 맞춘다
        theme = group.get("theme") if group.get("theme") in themes else (themes[n] if n < len(themes) else None)
        if theme is None:
            continue
        try:
            angles = parse_angles(json.dumps({"angles": group["angles"]}, ensure_ascii=False))
        except ValueError:
            continue
        for angle in angles:
            if len(angle.points) >= MIN_POINTS and violates(angle.text()) is None:
                items.append(BankItem(item_id(fmt, persona, theme, angle.title), theme, angle.title,
                                      angle.points, fmt, persona))
    return items


def _chunks(values: list, size: int) -> list[list]:
    return [values[i:i + size] for i in range(0, len(values), size)]


async def _generate(llm, themes: list[str], per_theme: int, guide: str, persona: dict | None,
                    fmt: str, persona_id: str, violates, retries: int = 2) -> list[BankItem]:
    prompt = build_prompt(themes, per_theme, guide, persona)
    for attempt in range(1 + retries):
        try:
            return parse_items(await llm.complete(prompt, max_tokens=MAX_TOKENS), themes, fmt, persona_id, violates)
        except (LlmError, ValueError, OSError) as exc:
            log.warning("%s.%s %s… 실패 (%d번째): %s", fmt, persona_id, themes[0], attempt + 1, exc)
    return []


def _write_items(path: Path, items: list[BankItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for item in items:
            f.write(json.dumps({"id": item.id, "theme": item.theme, "title": item.title,
                                "points": list(item.points), "format": item.format, "persona": item.persona},
                               ensure_ascii=False) + "\n")


async def build(args: argparse.Namespace) -> None:
    seed = yaml.safe_load((args.dir / SEED_FILE).read_text(encoding="utf-8"))
    themes: list[str] = seed["themes"]
    personas = {pid: yaml.safe_load((args.personas_dir / f"{pid}.yaml").read_text(encoding="utf-8"))
                for pid in seed["personas"]}
    (args.dir / PERSONAS_FILE).write_text(
        "# onair-bank build가 config/personas에서 옮겨 적는다 — 호스트의 페르소나와 견줄 때 쓴다. 직접 고치지 않는다\n"
        + yaml.safe_dump({pid: persona_text(p["concept"], p["tone"]) for pid, p in personas.items()},
                         allow_unicode=True, sort_keys=False),
        encoding="utf-8", newline="\n")
    llm = make_llm(args.llm or "dummy", model=args.llm_model, base_url=args.llm_base_url,
                   reasoning_effort=args.llm_reasoning_effort, timeout=LLM_TIMEOUT_SEC)
    violates = SafetyChecker(args.safety_rules).check_l2
    gate = asyncio.Semaphore(args.parallel)

    async def cell(fmt: str, spec: dict, persona_id: str) -> None:
        async def one(chunk: list[str]) -> list[BankItem]:
            async with gate:
                return await _generate(llm, chunk, spec["per_theme"], spec["guide"], personas.get(persona_id),
                                       fmt, persona_id, violates)
        batches = await asyncio.gather(*(one(chunk) for chunk in _chunks(themes, args.themes_per_call)))
        seen: set[str] = set()
        items = [item for batch in batches for item in batch
                 if item.id not in seen and not seen.add(item.id)]
        _write_items(args.dir / ITEMS_DIR / f"{fmt}.{persona_id}.jsonl", items)
        short = sorted({t for t in themes} - {item.theme for item in items})
        print(f"{fmt}.{persona_id}: {len(items)}건" + (f" — 비어 있는 주제 {len(short)}개: {short[:5]}" if short else ""))

    cells = [(fmt, spec, pid) for fmt, spec in seed["formats"].items() for pid in [COMMON, *personas]
             if not args.only or f"{fmt}.{pid}" in args.only]
    await asyncio.gather(*(cell(*c) for c in cells))


def _bank(args: argparse.Namespace) -> TalkBank:
    return TalkBank(load_items(args.dir), load_personas(args.dir), make_embedder(args.embedder),
                    cache_path=args.cache)


async def index(args: argparse.Namespace) -> None:
    bank = _bank(args)
    made = await bank.index()
    counts: dict[str, int] = {}
    for item in bank.items:
        counts[f"{item.format}.{item.persona}"] = counts.get(f"{item.format}.{item.persona}", 0) + 1
    print(f"이야깃거리 {len(bank.items)}건, 벡터 {made}건을 새로 만들었다 ({args.cache})")
    for name, count in sorted(counts.items()):
        print(f"  {name}: {count}")


async def search(args: argparse.Namespace) -> None:
    bank = _bank(args)
    persona = ""
    if args.persona:
        profile = yaml.safe_load(args.persona.read_text(encoding="utf-8"))
        persona = persona_text(profile["concept"], profile["tone"])
    if args.min_score is not None:
        bank.min_score = args.min_score
    found = await bank.search(args.topic, fmt=args.format, persona=persona, k=args.k)
    print(f"닮은 프리셋: {found.persona or '-'}")
    for hit in found.hits:
        print(f"{hit.score:.3f}  [{hit.item.persona}] {hit.item.theme} — {hit.item.title}")
        if args.points:
            for point in hit.item.points:
                print(f"         - {point}")


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="onair-bank", description="이야깃거리 데이터셋 만들기·색인·검색 (#332)")
    parser.add_argument("--dir", type=Path, default=Path("config/talk_bank"), help="데이터셋 폴더")
    parser.add_argument("--cache", type=Path, default=Path("var/talk_bank.sqlite"), help="벡터 캐시")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="씨앗(themes.yaml)에서 이야깃거리를 만든다 — LLM을 부른다")
    add_llm_args(b)
    b.add_argument("--personas-dir", type=Path, default=Path("config/personas"))
    b.add_argument("--safety-rules", type=Path, default=Path("config/safety_rules.yaml"))
    b.add_argument("--only", action="append", default=[], metavar="형식.페르소나",
                   help="이 칸만 다시 만든다 (예: talk.common, music.saebyeok). 반복 지정")
    b.add_argument("--themes-per-call", type=int, default=5, help="LLM 호출 한 번에 묻는 주제 수")
    b.add_argument("--parallel", type=int, default=4, help="동시에 부르는 LLM 호출 수")

    i = sub.add_parser("index", help="벡터 캐시를 채운다 — 캐시에 없는 것만 임베딩한다")
    s = sub.add_parser("search", help="주제로 찾아 본다")
    s.add_argument("topic")
    s.add_argument("--format", default="talk")
    s.add_argument("--persona", type=Path, default=None, metavar="FILE", help="persona 파일 — 닮은 프리셋을 고른다")
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--min-score", type=float, default=None, help="이 유사도 아래는 버린다 (기본: 엔진과 같은 값)")
    s.add_argument("--points", action="store_true", help="말할 거리까지 보여 준다")
    for p in (i, s):
        p.add_argument("--embedder", choices=["dummy", "gemini"], default="dummy")

    args = parser.parse_args(argv)
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 기본 인코딩(cp949)에서 깨지지 않게
    asyncio.run({"build": build, "index": index, "search": search}[args.command](args))


if __name__ == "__main__":
    cli()
