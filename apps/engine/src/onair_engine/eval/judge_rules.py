"""심사에 쓰는 방송 기록과, 코드로 재는 심사 항목 (config/judge_rubric.yaml의 by: rule·number).

방송 기록은 시나리오 실행기의 실행 폴더 하나다(played.json, run.json, report.json). 심사 AI에게는 멘트·청취자 글·곡에
M·C·S 번호를 붙인 기록을 보여 주고, 찾은 문제는 그 번호로 받는다. 여기 검사들도 같은 번호로 답한다.

규칙으로 잴 수 있는 것은 심사 AI에 맡기지 않는다 — 같은 글이 몇 번 인용됐는지, 무음이 몇 초였는지는 세면 된다.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from ..pipeline.normalize import normalize_for_tts
from ..pipeline.repeats import _pairs, _words
from .checks import expected_level, leaked_markup, level_violations, sentences

ENGINE_ROOT = Path(__file__).resolve().parents[3]
KST = timezone(timedelta(hours=9))
_INPUT_RE = re.compile(r"^(채팅|문자|신청곡) (\S+) \(([^)]*)\): (.*)$", re.DOTALL)
_SONG_CORNERS = ("music_intro", "closing")  # 곡을 소개하는 코너


@dataclass
class Ment:
    id: str  # M1, M2, ...
    at: float  # 방송 시작부터 흐른 초
    sec: float
    corner_type: str
    corner: str
    script: str | None  # 대본 기록이 없는 고정 문구는 None
    takes: list[str] = field(default_factory=list)  # 이 멘트가 소재로 쓴 청취자 글 (C 번호)
    after_song: bool = False  # 곡이 끝난 뒤의 첫 멘트


@dataclass
class Chat:
    id: str  # C1, C2, ...
    at: float
    kind: str  # 채팅 | 문자 | 신청곡
    request_id: str
    who: str  # L1, L2, ... — 유형 이름이 드러나지 않게 바꾼 청취자 이름
    body: str
    after: str | None = None  # 이 글을 쓸 때 듣던 것 (M·S 번호) — 가상 청취자의 기록이 있을 때만


@dataclass
class Song:
    id: str  # S1, S2, ...
    at: float
    sec: float
    title: str  # "제목 - 가수"


@dataclass
class Finding:
    at: str | None  # M·C·S·L 번호. 방송 전체에 대한 것이면 None
    why: str
    quote: str = ""

    def as_dict(self) -> dict:
        return {"at": self.at, "quote": self.quote, "why": self.why}


@dataclass
class Broadcast:
    path: Path
    ments: list[Ment]
    chats: list[Chat]
    songs: list[Song]
    order: list[Ment | Chat | Song]  # 방송에 나간 순서
    persona: dict
    topic: str
    started_at: float | None  # epoch 초
    minutes: float | None
    music_taste: list[str]
    tracks: list[str]
    silence_max_sec: float
    fallbacks: int
    scenario: str = ""
    run: int = 1

    @property
    def scripted(self) -> list[Ment]:
        return [m for m in self.ments if m.script]

    @property
    def listeners(self) -> list[str]:
        return list(dict.fromkeys(c.who for c in self.chats))

    @property
    def has_ending(self) -> bool:
        return any(m.corner_type == "closing" for m in self.ments)

    def ment(self, ment_id: str) -> Ment | None:
        return next((m for m in self.ments if m.id == ment_id), None)

    def chat(self, chat_id: str) -> Chat | None:
        return next((c for c in self.chats if c.id == chat_id), None)

    def taken_by(self, chat_id: str) -> list[Ment]:
        return [m for m in self.ments if chat_id in m.takes]

    def units(self, unit: str) -> int:
        return {"ment": len(self.scripted), "chat": len(self.chats), "listener": len(self.listeners)}.get(unit, 1)

    def local_start(self) -> datetime | None:
        return datetime.fromtimestamp(self.started_at, KST) if self.started_at else None


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _persona(run: dict) -> dict:
    ref = run.get("persona") or (run.get("scenario") or {}).get("station", {}).get("persona")
    path = ENGINE_ROOT / ref if ref else None
    if path and path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return {}


def load_broadcast(path: str | Path) -> Broadcast:
    """실행 폴더 하나(played.json이 있는 곳)를 읽는다."""
    path = Path(path)
    played = _read(path / "played.json")["played"]
    run, report = _read(path / "run.json"), _read(path / "report.json")
    scenario = run.get("scenario") or {}
    station = scenario.get("station") or {}

    heard_at_write = {s["request_id"]: s.get("after") or ""
                      for listener in (report.get("sim") or {}).get("audience") or []
                      for s in listener.get("sent") or []}
    ments, chats, songs, order = [], [], [], []
    by_request: dict[str, str] = {}
    names: dict[str, str] = {}
    pending: list[tuple[Ment, list[str]]] = []
    after_song = False
    for entry in played:
        kind = entry["kind"]
        if kind == "speech":
            ment = Ment(f"M{len(ments) + 1}", entry["at"], entry["sec"], entry.get("corner_type") or "",
                        entry.get("corner") or entry.get("corner_type") or "", (entry.get("script") or "").strip() or None,
                        after_song=after_song)
            after_song = False
            pending.append((ment, list(entry.get("reflected") or [])))
            ments.append(ment)
            order.append(ment)
        elif kind == "music":
            song = Song(f"S{len(songs) + 1}", entry["at"], entry["sec"], entry.get("text") or "")
            after_song = True
            songs.append(song)
            order.append(song)
        elif kind == "input":
            match = _INPUT_RE.match(entry.get("text") or "")
            if not match:
                continue
            label, request_id, who, body = match.groups()
            sender = re.split(r"[·,]", who)[0].strip()
            name = names.setdefault(sender, f"L{len(names) + 1}")
            chat = Chat(f"C{len(chats) + 1}", entry["at"], label, request_id, name, body.strip())
            heard = heard_at_write.get(request_id)
            if heard:
                chat.after = _find_heard(heard, ments, songs)
            by_request[request_id] = chat.id
            chats.append(chat)
            order.append(chat)
    for ment, request_ids in pending:
        ment.takes = [by_request[r] for r in request_ids if r in by_request]

    sim, generation = report.get("sim") or {}, report.get("generation") or {}
    return Broadcast(
        path=path, ments=ments, chats=chats, songs=songs, order=order, persona=_persona(run),
        topic=report.get("topic") or station.get("topic") or "", started_at=report.get("started_at"),
        minutes=run.get("minutes") or station.get("broadcast_minutes"),
        music_taste=list(station.get("music_taste") or []),
        tracks=[f"{t['title']} - {t['artist']}" for t in scenario.get("tracks") or []],
        silence_max_sec=float(sim.get("silence_max_sec") or 0),
        fallbacks=int((generation.get("outcomes") or {}).get("fallback") or 0),
        scenario=scenario.get("name") or path.parent.name, run=int(run.get("run") or 1))


def _find_heard(heard: str, ments: list[Ment], songs: list[Song]) -> str | None:
    """가상 청취자가 글을 쓸 때 듣던 것(대본 글)을 M·S 번호로 바꾼다. 가장 최근 것부터 찾는다."""
    if heard.startswith("(곡) "):
        title = heard[4:].strip()
        return next((s.id for s in reversed(songs) if s.title == title), None)
    return next((m.id for m in reversed(ments) if m.script == heard.strip()), None)


# ── 코드로 재는 항목 ──────────────────────────────────────────────────

def _squash(text: str) -> str:
    return "".join(text.split()).lower()


def restart(b: Broadcast, **_) -> list[Finding]:
    """I5 다시 시작 — 오프닝이 아닌데 방송을 새로 여는 인사를 한다.

    DJ 이름이 흔한 낱말일 수 있어("서두르지 않아도 되는 새벽입니다") 이름은 "…의 새벽입니다", "…, 새벽입니다" 꼴만 본다.
    청취자 글을 읽으면서 나온 인사("새벽님 안녕하세요!")는 DJ의 인사가 아니다.
    """
    name = re.escape(b.persona.get("dj_name") or "\0")
    pattern = re.compile(rf"안녕하(세요|십니까)|(방송|라디오)[^.!?]{{0,14}}시작(합니다|할게요|해요|하겠습니다)"
                         rf"|(의|,)\s*{name}(입니다|이에요|예요)")
    out = []
    for m in b.scripted:
        if m.corner_type == "opening":
            continue
        hit = pattern.search(m.script)
        if hit and not any(hit.group() in b.chat(cid).body for cid in m.takes):
            out.append(Finding(m.id, "오프닝이 아닌 자리에서 방송을 새로 시작하는 인사를 한다", hit.group()))
    return out


def repeated_quote(b: Broadcast, *, min_ments: int = 3, **_) -> list[Finding]:
    """B7 되풀이 인용 — 같은 청취자 글을 여러 멘트에서 소재로 쓴다."""
    out = []
    for chat in b.chats:
        ments = b.taken_by(chat.id)
        if len(ments) >= min_ments:
            out.append(Finding(chat.id, f"같은 글을 멘트 {len(ments)}개({', '.join(m.id for m in ments)})에서 썼다",
                               chat.body[:60]))
    return out


def one_listener(b: Broadcast, *, min_listeners: int = 3, min_taken: int = 4, share: float = 0.6,
                 over: float = 0.25, **_) -> list[Finding]:
    """B8 한 사람 쏠림 — 한 청취자의 글만 유난히 많이 받는다 (보낸 글의 몫보다 받은 몫이 훨씬 크다)."""
    sent = Counter(c.who for c in b.chats)
    taken = Counter(b.chat(cid).who for m in b.ments for cid in m.takes)
    total = sum(taken.values())
    if len(sent) < min_listeners or total < min_taken:
        return []
    who, count = taken.most_common(1)[0]
    got, gave = count / total, sent[who] / len(b.chats)
    if got >= share and got - gave >= over:
        return [Finding(None, f"받은 글 {total}건 가운데 {count}건이 {who}의 글이다 (이 청취자가 보낸 글은 전체의 {gave:.0%})")]
    return []


def breaks(b: Broadcast, *, max_silence_sec: float = 5.0, **_) -> list[Finding]:
    """C4 끊김 — 긴 무음, 대체 멘트."""
    out = []
    if b.silence_max_sec > max_silence_sec:
        out.append(Finding(None, f"무음이 {b.silence_max_sec:.1f}초 이어졌다"))
    if b.fallbacks:
        out.append(Finding(None, f"대본을 만들지 못해 대체 멘트가 {b.fallbacks}번 나갔다"))
    return out


def repeated_phrase(b: Broadcast, *, min_ments: int = 4, **_) -> list[Finding]:
    """D3 되풀이 — 같은 어절 묶음(3~4어절)이 여러 멘트에 나온다. 청취자 글·곡 제목·주제에 있는 말은 세지 않는다."""
    given = _pairs(_words(" ".join([b.topic, *b.tracks, *(s.title for s in b.songs), *(c.body for c in b.chats)])))
    seen: dict[str, list[str]] = {}
    for m in b.scripted:
        words = _words(m.script)
        for size in (4, 3):
            for i in range(len(words) - size + 1):
                chunk = words[i:i + size]
                if not _pairs(chunk) & given:
                    ids = seen.setdefault(" ".join(chunk), [])
                    if m.id not in ids:
                        ids.append(m.id)
    hits = sorted(((p, ids) for p, ids in seen.items() if len(ids) >= min_ments),
                  key=lambda item: (-len(item[0].split()), -len(item[1])))
    out, kept = [], []
    for phrase, ids in hits:
        if any(_pairs(phrase.split()) & _pairs(longer.split()) for longer in kept):
            continue  # 이미 적은 묶음의 일부다
        kept.append(phrase)
        out.append(Finding(ids[0], f"'{phrase}'가 멘트 {len(ids)}개에 나온다 ({', '.join(ids[:8])})", phrase))
    return out


def long_sentence(b: Broadcast, *, max_chars: int = 60, **_) -> list[Finding]:
    """A2 호흡 — 한 문장이 너무 길다 (띄어쓰기와 문장부호를 뺀 글자 수)."""
    out = []
    for m in b.scripted:
        long = [s for s in sentences(m.script) if sum(ch.isalnum() for ch in s) > max_chars]
        if long:
            out.append(Finding(m.id, f"한 문장이 {sum(ch.isalnum() for ch in long[0])}자다", long[0]))
    return out


def speech_level(b: Broadcast, **_) -> list[Finding]:
    """E1 말투 유지 — DJ 설정의 말투(존댓말·반말)와 반대로 끝난 문장."""
    expected = expected_level(b.persona.get("tone") or "")
    source = " ".join([*(c.body for c in b.chats), *(s.title for s in b.songs), *b.tracks])
    out = []
    for m in b.scripted:
        wrong = level_violations(m.script, expected, source)
        if wrong:
            out.append(Finding(m.id, "DJ 설정과 다른 말투로 끝난 문장이 있다", wrong[0]))
    return out


def song_title(b: Broadcast, **_) -> list[Finding]:
    """G3 이름과 제목 — 곡을 소개하는 멘트에 그 곡의 제목이 적힌 그대로 없다."""
    out = []
    for i, item in enumerate(b.order):
        if not isinstance(item, Song) or " - " not in item.title:
            continue
        before = next((x for x in reversed(b.order[:i]) if not isinstance(x, Chat)), None)
        if not isinstance(before, Ment) or not before.script or before.corner_type not in _SONG_CORNERS:
            continue
        title = item.title.rsplit(" - ", 1)[0]
        if _squash(title) not in _squash(before.script):
            out.append(Finding(before.id, f"바로 뒤에 나간 곡은 '{item.title}'인데 소개에 그 제목이 그대로 없다",
                               before.script[:80]))
    return out


_UNREADABLE = re.compile(r"https?://|www\.|[@^|\\=+—–]")


def unreadable(b: Broadcast, **_) -> list[Finding]:
    """H1 읽을 수 없는 문자 — TTS 직전 정규화를 거친 뒤에도 남는 기호·그림 문자·주소."""
    out = []
    for m in b.scripted:
        text = normalize_for_tts(m.script)
        marks = leaked_markup(text) + _UNREADABLE.findall(text)
        if marks:
            out.append(Finding(m.id, "음성 합성이 읽을 수 없는 문자가 있다: " + " ".join(dict.fromkeys(marks)), marks[0]))
    return out


def long_ment(b: Broadcast, *, max_sec: float = 30.0, **_) -> list[Finding]:
    """H3 길이 — 멘트 하나가 너무 길다."""
    return [Finding(m.id, f"멘트 하나가 {m.sec:.0f}초다", (m.script or "")[:60])
            for m in b.ments if m.sec > max_sec]


CHECKS = {
    "restart": restart, "repeated_quote": repeated_quote, "one_listener": one_listener, "breaks": breaks,
    "repeated_phrase": repeated_phrase, "long_sentence": long_sentence, "speech_level": speech_level,
    "song_title": song_title, "unreadable": unreadable, "long_ment": long_ment,
}