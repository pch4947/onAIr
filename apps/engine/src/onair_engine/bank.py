"""onair-bank — 이야깃거리 데이터셋을 만들고, 벡터를 미리 채우고, 검색을 눈으로 확인한다 (#332).

    onair-bank build --llm gemini --llm-model gemini-3.8-flash   # 씨앗(themes.yaml)에서 items/를 만든다
    onair-bank index --embedder gemini                            # 벡터 캐시를 미리 채운다 (배포 뒤 한 번)
    onair-bank search "시험 기간을 버티는 나만의 방법" --embedder gemini --persona config/personas/saebyeok.yaml

데이터셋은 외부 글을 긁지 않고 LLM으로 만든다. 주제 × 페르소나(프리셋 + 공통) × 형식으로 묻고, 방송에 나갈
말의 재료이므로 대본과 같은 L2 안전 규칙을 통과한 것만 남긴다. 쓰는 쪽은 inputs/talk_bank.py다.

같은 주제를 페르소나마다 따로 물으면 같은 이야기가 돌아온다 (2026-11-19 실측: 한 주제의 토크 24건 중 서로 다른
이야기는 14건). 그래서 (1) 칸마다 이야기를 푸는 방식(kinds)을 나눠 맡기고, (2) 같은 주제에 이미 만든 갈래의
제목을 보여 주고, (3) --embedder를 주면 이미 있는 갈래와 닮은 것(유사도 0.92 이상)을 버리고 그 자리만 다시 묻는다.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from collections import Counter
from pathlib import Path

import yaml

from .domain import Prompt
from .inputs.talk_bank import (
    COMMON,
    ITEMS_DIR,
    PERSONAS_FILE,
    BankItem,
    TalkBank,
    _VectorCache,
    load_items,
    load_personas,
    persona_text,
)
from .inputs.topic_plan import parse_angles
from .main import add_llm_args, load_env
from .pipeline.embed import EmbedError, dot, make_embedder
from .pipeline.llm import LlmError, make_llm
from .pipeline.safety import SafetyChecker
from .prompt_data import DATA_RULES, STATION_CONCEPT, data_block

log = logging.getLogger(__name__)

SEED_FILE = "themes.yaml"
THEMES = "주제 목록"
EXISTING = "이미 있는 갈래"
MIN_POINTS = 3  # 말할 거리가 이보다 적은 갈래는 버린다 — 주제 토크 한 번을 채우지 못한다
MAX_TOKENS = 16000
LLM_TIMEOUT_SEC = 180.0  # 한 번에 갈래 수십 개를 받는다 — 대본용 기본값(20초)으로는 끊긴다
# 같은 주제에 이미 있는 갈래와 이만큼 닮으면 같은 이야기로 보고 버린다 — 방송 간 재사용 규칙과 같은 값
# (inputs/topic_usage.py). 이 값에서 같은 이야기 28쌍 중 17쌍이 걸리고 다른 이야기 150쌍 중 8쌍이 잘못 걸린다 (T-017)
DEDUP = 0.92
ROUNDS = 3  # 버려져 빈 자리를 다시 묻는 횟수 — 그래도 비면 비워 둔다 (억지로 채우면 겹친다)
_SYSTEM = "너는 라디오 방송 구성 작가다. DJ가 방송에서 꺼낼 이야깃거리를 미리 만들어 둔다.\n" + DATA_RULES


def kinds_for(kinds: list[str], per_theme: int, theme_no: int, persona_no: int) -> list[str]:
    """주제 하나에서 한 칸이 맡는 이야기 방식 per_theme개. persona_no는 공통이 0, 프리셋이 1부터.

    방송 하나는 공통과 프리셋 하나의 것을 함께 본다 — 그 둘을 합치면 방식이 고루 나오게 나눈다.
    공통이 앞의 것을 맡고, 프리셋은 공통이 맡지 않은 것부터 맡는다. 주제마다 시작을 돌려 한 방식이 공통에만 몰리지 않는다.
    """
    if not kinds:
        return [""] * per_theme
    start = theme_no % len(kinds)
    order = kinds[start:] + kinds[:start]
    head, rest = order[:per_theme], order[per_theme:]
    if persona_no == 0 or not rest:
        return [order[n % len(order)] for n in range(per_theme)]
    turn = (persona_no - 1) % len(head)
    return (rest + head[turn:] + head[:turn])[:per_theme]


def build_prompt(slots: dict[str, list[str]], guide: str, persona: dict | None, *,
                 kinds: dict[str, str] | None = None, existing: dict[str, list[str]] | None = None) -> Prompt:
    """slots는 주제 → 만들 갈래의 방식들(방식을 나누지 않으면 빈 문자열을 갈래 수만큼).

    persona는 프리셋 파일의 내용 — None이면 어느 방송에나 쓰는 공통 이야깃거리를 만든다.
    existing은 주제 → 그 주제에 이미 만들어 둔 갈래의 제목.
    """
    typed = bool(kinds) and all(kind for wanted in slots.values() for kind in wanted)
    if typed:
        lines = ["이야깃거리 데이터셋: 아래 주제 목록의 주제마다, 옆에 적힌 방식의 이야기 갈래를 방식마다 하나씩 만든다."]
    else:
        count = max(len(wanted) for wanted in slots.values())
        lines = [f"이야깃거리 데이터셋: 아래 주제마다 서로 겹치지 않는 이야기 갈래 {count}개를 만든다."]
    lines += [
        f"- {guide}",
        "- 갈래마다 짧은 제목 하나와 DJ가 말할 거리 4~6개를 붙인다.",
        "- 말할 거리는 순서대로 이어지게 쓴다. 앞의 것으로 이야기를 꺼내고, 가운데에서 풀어 가고, 마지막 것으로 마무리한다.",
        "- 말할 거리는 구체적으로 쓴다. 질문 문장으로 쓰지 않는다.",
        ("- 말할 거리는 작가의 메모다. '~한다'로 끝나는 짧은 평서문이나 명사로 끝나는 구로 쓴다. "
         "존댓말(~합니다, ~해요)과 말을 거는 말끝(~죠, ~네요)은 쓰지 않는다 — 말투는 DJ가 입힌다."),
        "- 한 갈래 안에서 같은 낱말로 끝나는 말할 거리를 두 개 넘게 두지 않는다.",
        "- 수치, 가격, 상호, 날짜, 역사적 사실, 실제 인물과 작품 이름처럼 확인할 수 없는 사실은 넣지 않는다.",
        "- DJ가 직접 겪은 일처럼 쓰지 않는다.",
        "- 같은 주제의 갈래끼리는 소재가 달라야 한다. 한 갈래를 말만 바꿔 되풀이하지 않는다.",
    ]
    if typed:
        used = list(dict.fromkeys(kind for wanted in slots.values() for kind in wanted))
        lines.append("- 방식은 이야기를 푸는 길이다. 갈래의 kind에 방식 이름을 그대로 적는다:")
        lines += [f"  - {kind}: {kinds[kind]}" for kind in used]
        themes_text = "\n".join(f"- {theme}: {', '.join(wanted)}" for theme, wanted in slots.items())
        shape = '{"kind": "방식 이름", "title": "갈래 제목", "points": ["말할 거리", ...]}'
    else:
        themes_text = "\n".join(f"- {theme}" for theme in slots)
        shape = '{"title": "갈래 제목", "points": ["말할 거리", ...]}'
    blocks = [data_block(THEMES, themes_text)]
    if persona is None:
        lines.append("- 어느 방송에서나 쓸 수 있게 쓴다. 특정 시간대나 청취자의 처지에 기대지 않는다.")
    else:
        lines.append("- 아래 스테이션 컨셉의 청취자가 지금 겪고 있을 장면으로 쓴다. 듣는 시간대와 처지를 살린다. "
                     "DJ의 말투는 흉내 내지 않는다 — 말할 내용만 쓴다.")
        if persona.get("forbidden"):
            lines.append("- 다음은 넣지 않는다: " + "; ".join(persona["forbidden"]))
        about = [persona["concept"], persona.get("description", ""), persona["tone"]]
        blocks.append(data_block(STATION_CONCEPT, "\n".join(line for line in about if line)))
    existing = {theme: titles for theme, titles in (existing or {}).items() if titles and theme in slots}
    if existing:
        lines.append("- 아래 이미 있는 갈래는 같은 주제로 만들어 둔 것이다. 이것들과 소재가 겹치는 갈래는 만들지 않는다 — "
                     "같은 장면을 말만 바꾸거나 시간대만 옮긴 것도 겹치는 것이다.")
        blocks.append(data_block(EXISTING, "\n".join(f"- {theme}: {' / '.join(titles)}"
                                                      for theme, titles in existing.items())))
    lines += [f'- 출력은 JSON 하나만: {{"themes": [{{"theme": "주제 그대로", "angles": [{shape}]}}]}}', "", *blocks]
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
        for raw in group["angles"]:
            try:  # 하나씩 읽는다 — 틀린 갈래 하나가 그 주제의 나머지를 버리지 않게
                (angle,) = parse_angles(json.dumps({"angles": [raw]}, ensure_ascii=False))
            except ValueError:
                continue
            kind = raw.get("kind") if isinstance(raw.get("kind"), str) else ""
            if len(angle.points) >= MIN_POINTS and violates(angle.text()) is None:
                items.append(BankItem(item_id(fmt, persona, theme, angle.title), theme, angle.title,
                                      angle.points, fmt, persona, kind.strip()))
    return items


def _chunks(values: list, size: int) -> list[list]:
    return [values[i:i + size] for i in range(0, len(values), size)]


async def _generate(llm, prompt: Prompt, themes: list[str], fmt: str, persona_id: str, violates,
                    retries: int = 1) -> list[BankItem]:
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
            row = {"id": item.id, "theme": item.theme, "title": item.title, "points": list(item.points),
                   "format": item.format, "persona": item.persona}
            if item.kind:
                row["kind"] = item.kind
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class _Pool:
    """만드는 동안의 데이터셋 — 주제마다 이미 있는 갈래를 들고, 새 갈래가 그것들과 겹치는지 본다."""

    def __init__(self, embedder, cache_path: Path | None, threshold: float):
        self._embedder = embedder
        self._cache = _VectorCache(cache_path, embedder.name) if embedder is not None else None
        self._threshold = threshold
        self._items: dict[tuple[str, str], list[BankItem]] = {}  # (형식, 주제) → 갈래
        self._vectors: dict[str, object] = {}  # id → 벡터
        self.dropped = 0

    def of(self, fmt: str, theme: str) -> list[BankItem]:
        return self._items.get((fmt, theme), [])

    def all(self) -> list[BankItem]:
        return [item for group in self._items.values() for item in group]

    async def _embed(self, items: list[BankItem]) -> None:
        """벡터 캐시에도 넣는다 — 다 만든 뒤 onair-bank index가 다시 임베딩하지 않는다."""
        # 제목이 같으면 id도 같다 — 먼저 받은 것의 벡터를 그대로 둔다 (뒤에 온 것은 제목이 같아 어차피 버린다)
        items = [item for item in items if item.id not in self._vectors]
        texts = list(dict.fromkeys(item.text() for item in items))
        known = self._cache.get(texts)
        missing = [text for text in texts if text not in known]
        for chunk in _chunks(missing, 100):
            made = dict(zip(chunk, await self._embedder.embed(chunk), strict=True))
            self._cache.put(made)
            known.update(made)
        for item in items:
            self._vectors.setdefault(item.id, known[item.text()])

    async def keep(self, items: list[BankItem]) -> None:
        """디스크에서 읽은 것 — 견주지 않고 그대로 둔다."""
        if self._embedder is not None:
            await self._embed(items)
        for item in items:
            self._items.setdefault((item.format, item.theme), []).append(item)

    async def add(self, items: list[BankItem], wanted: dict[str, list[str]]) -> None:
        """새로 만든 것 가운데 달라고 한 자리(wanted: 주제 → 남은 방식)에 맞고 겹치지 않는 것만 받는다.

        받은 만큼 wanted에서 지운다. 임베딩이 실패하면 제목이 같은 것만 거르고 받는다 — 만들기를 멈추지 않는다.
        """
        vectors_ok = self._embedder is not None
        if vectors_ok:
            try:
                await self._embed(items)
            except EmbedError as exc:
                log.warning("임베딩 실패, 이번 묶음은 제목만 견준다: %r", exc)
                vectors_ok = False
        for item in items:
            left = wanted.get(item.theme)
            if not left or item.kind not in left:
                continue
            group = self._items.setdefault((item.format, item.theme), [])
            vector = self._vectors.get(item.id) if vectors_ok else None
            if any(other.title == item.title for other in group) or (vector is not None and any(
                    other.id in self._vectors and dot(vector, self._vectors[other.id]) >= self._threshold
                    for other in group)):
                self.dropped += 1
                continue
            group.append(item)
            left.remove(item.kind)


async def build(args: argparse.Namespace) -> None:
    seed = yaml.safe_load((args.dir / SEED_FILE).read_text(encoding="utf-8"))
    themes: list[str] = seed["themes"]
    kinds: dict[str, str] = seed.get("kinds") or {}
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
    if args.embedder is None:
        print("--embedder가 없어 제목이 같은 것만 거른다. 닮은 갈래까지 거르려면 --embedder gemini")
    pool = _Pool(make_embedder(args.embedder) if args.embedder else None, args.cache, args.dedup)
    persona_ids = [COMMON, *personas]
    cells = [(fmt, pid) for fmt in seed["formats"] for pid in persona_ids]
    targets = [cell for cell in cells if not args.only or f"{cell[0]}.{cell[1]}" in args.only]
    # 그대로 두는 것: 다시 만들지 않는 칸의 것, --fill이면 이미 있는 것 전부. 씨앗에서 뺀 주제의 것은 버린다
    on_disk = load_items(args.dir) if (args.dir / ITEMS_DIR).is_dir() else []
    await pool.keep([item for item in on_disk if item.theme in themes and (item.format, item.persona) in cells
                     and (args.fill or (item.format, item.persona) not in targets)])

    def missing(fmt: str, pid: str, chunk: list[str]) -> dict[str, list[str]]:
        """주제 → 이 칸에 아직 없는 방식들."""
        per_theme = seed["formats"][fmt]["per_theme"]
        wanted = {}
        for theme in chunk:
            have = [item for item in pool.of(fmt, theme) if item.persona == pid]
            left = Counter(kinds_for(list(kinds), per_theme, themes.index(theme), persona_ids.index(pid)))
            left -= Counter(item.kind for item in have)
            room = per_theme - len(have)
            if room > 0 and left:
                wanted[theme] = list(left.elements())[:room]
        return wanted

    def save() -> None:
        order = {theme: n for n, theme in enumerate(themes)}
        for fmt, pid in cells:
            items = sorted((item for item in pool.all() if (item.format, item.persona) == (fmt, pid)),
                           key=lambda item: order[item.theme])
            _write_items(args.dir / ITEMS_DIR / f"{fmt}.{pid}.jsonl", items)

    gate = asyncio.Semaphore(args.parallel)

    async def column(fmt: str, chunk: list[str]) -> None:
        """한 형식의 주제 한 묶음 — 칸(페르소나)을 차례로 만든다.

        뒤 칸이 앞 칸의 갈래를 보고 피해야 하므로 칸끼리는 겹쳐 돌리지 않는다.
        """
        spec = seed["formats"][fmt]
        async with gate:
            for pid in (pid for f, pid in targets if f == fmt):
                for _ in range(1 + ROUNDS):
                    wanted = missing(fmt, pid, chunk)
                    if not wanted:
                        break
                    existing = {theme: [item.title for item in pool.of(fmt, theme)] for theme in wanted}
                    prompt = build_prompt(wanted, spec["guide"], personas.get(pid), kinds=kinds, existing=existing)
                    await pool.add(await _generate(llm, prompt, list(wanted), fmt, pid, violates), wanted)
            save()  # 묶음마다 적어 둔다 — 도중에 끊기면 --fill로 이어서 만든다
            print(f"  {fmt} {chunk[0]} … {chunk[-1]} ({len(pool.all())}건)", flush=True)

    await asyncio.gather(*(column(fmt, chunk) for fmt in dict.fromkeys(f for f, _ in targets)
                           for chunk in _chunks(themes, args.themes_per_call)))
    save()
    for fmt, pid in targets:
        items = [item for item in pool.all() if (item.format, item.persona) == (fmt, pid)]
        short = sum(len(wanted) for wanted in missing(fmt, pid, themes).values())
        print(f"{fmt}.{pid}: {len(items)}건" + (f" — 빈 자리 {short}개" if short else ""))
    print(f"전체 {len(pool.all())}건, 이미 있는 갈래와 겹쳐 버린 것 {pool.dropped}건")


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
    b.add_argument("--fill", action="store_true",
                   help="이미 있는 이야깃거리는 그대로 두고 빈 자리만 만든다 — 끊긴 뒤 잇기, 주제를 더한 뒤")
    b.add_argument("--embedder", choices=["dummy", "gemini"], default=None,
                   help="같은 주제에 이미 있는 갈래와 닮은 것을 버리고 다시 묻는다. 없으면 제목이 같은 것만 거른다")
    b.add_argument("--dedup", type=float, default=DEDUP, help="이 유사도 이상이면 같은 이야기로 본다")
    b.add_argument("--themes-per-call", type=int, default=5, help="LLM 호출 한 번에 묻는 주제 수")
    b.add_argument("--parallel", type=int, default=4, help="동시에 만드는 주제 묶음 수")

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
