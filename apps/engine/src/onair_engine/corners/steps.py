"""단계별 프롬프트 빌더 — 코너는 설정(config/formats)이 정하고, 단계 유형마다 지시문은 여기서 만든다.

외부 텍스트(곡 정보, 기사, 사연, 청취자 채팅, 오늘의 주제)는 전부 데이터 블록으로 넣는다 (prompt_data.py).
지시문마다 marker 구절이 하나씩 들어간다 — 더미 LLM이 프롬프트만 보고 단계를 알아보는 표식이다
(pipeline/llm.py detect_step). 지시문을 고칠 때 marker를 지우지 않는다 (test_steps가 검사한다).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ..domain import TONE_JOKE, Material, Prompt, StationProfile
from ..prompt_data import (
    ANGLE,
    ASKED,
    ASKED_BEFORE,
    CALLBACK,
    CHAT,
    CORNERS,
    COVERED_ANGLES,
    DILEMMA,
    HEARD,
    NOW,
    PICK_WHY,
    REACTIONS,
    SOURCES,
    STORY,
    TOPIC,
    TRACK,
    TREND,
    VOTE_RESULT,
    data_block,
)
from .base import system_prompt

# 채팅 인용 지시 — 인용할지 말지를 LLM에 맡기지 않는다. 넣었으면 반드시 인용한다 (CORNER_DESIGN 5.1절)
# 질문은 답만 하고 누가 물었는지 빼먹기 쉽다 — 2026-11-12 평가에서 DJ에게 묻는 채팅 36건 중 12건이 그랬다 (T-007)
# 예시에 문장 끝을 넣지 않는다 — 넣으면 그 말끝을 그대로 따라 쓴다. 2026-11-12 평가에서 '~분이 있네요'는
# 반말 DJ의 채팅 멘트 60건 모두에, '~분이 있다'는 존댓말 DJ의 60건 중 20건에 그대로 나왔다 (T-007)
# 질문을 옮기며 무엇에 대한 질문인지를 빼면("저도 꼬리부터 먹느냐고 물어보신 분") 듣는 사람이 알 수 없다 (T-019)
# 예시가 하나뿐이면 채팅을 받는 멘트가 모두 그 꼴이 된다 — 방송 40개의 채팅 멘트 230건 가운데 82%가 "~라고 하신 분"
# 이나 "~냐고 물어보신 분"이었다(실행 20261119-221335). 예시는 대본에 그대로 나오므로, 예시를 멘트마다 돌린다 (T-021)
SAID_FORM, ASKED_FORM = "~라고 하신 분", "~냐고 물어보신 분"
_QUOTE_RULE = ("아래 청취자 채팅을 짧게 인용하고 한 문장으로 반응한다. 인용은 '{said}'처럼 청취자의 말임을 밝힌다. "
               "채팅이 질문이면 '{asked}'처럼 청취자가 물었다는 것부터 말하고 짧게 답한다. "
               "질문을 옮길 때는 무엇을 두고 묻는지(음식이나 곡의 이름)를 빼지 않는다. "
               "문장 끝은 DJ의 말투로 맺는다. 닉네임이나 아이디는 말하지 않는다.")
QUOTE_RULE = _QUOTE_RULE.format(said=SAID_FORM, asked=ASKED_FORM)
# 한 멘트에서 여러 건을 받을 때 건마다 같은 말로 밝히면 "~라고 하신 분, … ~라고 하신 분, …"이 된다
VARY_FORM_RULE = "여러 건을 받을 때는 건마다 밝히는 말을 달리한다 — 다음 건은 '{other}'처럼 밝힌다."
# 다음 멘트가 이 채팅을 화제로 이어 간다 (CORNER_DESIGN 5.2절). 예전에는 "이 얘기를 조금 더 해 보겠다"고 말하게
# 했는데, 그 말이 "겨울 간식 이야기를 더 나눠볼게요"처럼 오늘 주제를 예고하는 말로 나오고 다음 멘트는 채팅 이야기를
# 이어 가서 예고와 어긋났다 — 가상 청취자 방송 40개에서 이어 말하기 문제 51건 중 28건이 이것이었다 (T-016)
STEER_RULE = ("이 채팅 이야기는 다음 멘트에서 조금 더 이어 간다. 그러니 여기서 이야기를 마무리 짓지 않고, "
              "무엇을 더 이야기하겠다고 예고하는 말로 멘트를 끝내지도 않는다.")
# 한 멘트에 입력 여러 건 (계약 v2 input_refs, #267)
# 묶어서 "여러 분이 ~라고 하셨네요"라고 하면 누구의 말도 인용되지 않는다 — 한 건씩 밝혀야 자기 말인 줄 안다
MULTI_QUOTE_RULE = ("채팅이 여러 건이다. 한 건씩 따로 인용하고 각각 한 문장으로 반응한다. "
                    "한 건도 빠뜨리지 않고, 여러 건을 한 문장으로 묶지 않는다.")
MULTI_STEER_RULE = ("마지막에 인용한 채팅 이야기는 다음 멘트에서 조금 더 이어 간다. 그러니 여기서 그 이야기를 "
                    "마무리 짓지 않고, 무엇을 더 이야기하겠다고 예고하는 말로 멘트를 끝내지도 않는다.")
# 농담을 사실로 받으면 장난에 끌려간다 — "핫팩 먹어도 돼요?"에 먹지 말고 손에 쥐라고 답했다 (T-016)
JOKE_RULE = ("{which} 청취자가 웃자고 한 농담이다. 사실로 받아 진지하게 답하거나 설명·조언·당부를 하지 않는다. "
             "질문 꼴이어도 답하려 들지 않고, 농담인 줄 알고 가볍게 받아 한마디로 넘긴다.")
# 반응형 채팅은 한 건씩 인용하지 않는다 (2026-11-15 결정) — 멘트가 만들어져 나가는 사이 화제가 바뀌어,
# 앞 이야기에 대한 "맞는 것 같아요"가 지금 이야기에 대한 말처럼 인용됐다. 분위기만 전한다
# 반응 분위기는 곡 뒤 첫 멘트에서만 전한다 (5.1.5절). 이야기 도중에 전하면 하던 이야기에 끼어든다 — 방송 40개에서
# 이야기 도중·곡 소개에 전한 30번 중 13번이 지적됐고, 곡 뒤 첫 멘트에 전한 22번은 2번이었다 (T-019)
# 곡을 알려 주는 멘트(BACK_ANNOUNCE_RULE)에서는 그 문장 바로 뒤에 전한다 — 곡 감상이 곡 이름 뒤에 붙는다
_MOOD_RULE = ("아래는 곡이 나가기 전의 이야기와 방금 나간 곡을 들으며 청취자들이 보낸 반응이다. 한 건씩 인용하지 않고, "
             "어떤 반응이 있었는지 {where} 한 문장으로만 전한 뒤 하려던 이야기로 넘어간다. 반응이 여러 "
             "갈래면 다 늘어놓지 않고 두 가지까지만 고른다. 반응의 글귀를 "
             "그대로 읽지 않고, 숫자와 닉네임을 말하지 않는다. 반응이 한 건뿐이면 여러 사람인 것처럼 말하지 않는다.")
MOOD_RULE = _MOOD_RULE.format(where="멘트 첫머리에")
MOOD_AFTER_ANNOUNCE_RULE = _MOOD_RULE.format(where="곡을 알려 준 문장 바로 뒤에")
# 곡이 나가는 동안 올라온 반응은 곡에 대한 것일 수도, 곡 전에 한 말에 대한 것일 수도 있다. 곡만 알려 줬더니
# "그 말 맞는 것 같아요"를 곡에 대한 반응으로 전하고 "풍경을 떠올린 듯한 반응"처럼 뜻을 지어냈다 (T-012)
# "그때 나간 말에는…", "그 노래를 들으며…"처럼 가리키기만 하면 무엇에 대한 반응인지 알 수 없다 (T-019)
MOOD_HEARD_RULE = ("반응은 아래 그때 방송에 나가던 것을 듣고 보낸 것이다. 지금 하려는 이야기가 아니라 그 말이나 "
                   "곡에 대한 반응으로 전한다. 곡에 대한 반응이면 방금 나간 곡에 대한 것임을, 곡 전의 이야기에 대한 "
                   "반응이면 그 이야기가 무엇이었는지를 말에 담아, 듣는 사람이 무엇에 대한 반응인지 알게 한다. "
                   "'그때', '그 노래', '그 말'처럼 가리키기만 하는 말은 쓰지 않는다. 곡과 말이 함께 적혀 있으면 "
                   "반응의 내용을 보고 어느 쪽에 대한 것인지 판단하고, 분명하지 않은 반응은 전하지 않는다. "
                   "반응에 없는 뜻을 지어내지 않는다.")
# 방금 들은 말을 가리키거나 되묻는 채팅("그게 무슨 말이에요?") — 무엇을 가리키는지 알려 주지 않으면 답을 지어낸다
QUOTE_HEARD_RULE = ("채팅 가운데 방금 들은 말을 가리키거나 되묻는 것은 아래 그때 방송에 나가던 것을 듣고 보낸 것이다. "
                    "그 말을 두고 답하거나 반응한다.")
# 화제 블록이 멘트마다 들어가, 두면 멘트마다 주제를 다시 알리며 시작한다 ("수능까지 나흘 남았네요", T-012)
NO_RESTATE_RULE = "주제를 다시 알리는 말로 멘트를 시작하지 않는다. 청취자는 오늘의 주제를 이미 안다."
_MOOD_MAX_LINES = 8
_HEARD_MAX_CHARS = 400
# 채팅이 꺼낸 화제로 잇는 멘트 — 화제 블록에 그 채팅이 그대로 들어 있어, 두면 멘트마다 다시 인용한다
STEERED_RULE = ("아래 지금 화제는 앞 멘트에서 이미 인용한 청취자 채팅이다. 그 채팅을 다시 인용하거나 "
                "누가 물었다고 다시 말하지 않고, 그 내용으로 DJ의 이야기만 이어간다. "
                "앞 멘트에서 이미 한 답을 되풀이하지 않고, 거기에 덧붙일 다른 이야기를 한다.")
# 주 소재(사연·신청곡)가 있는 멘트 — 채팅 때문에 사연이 요약되거나 곡 소개가 밀리면 안 된다
WITH_FEATURE_RULE = ("이 멘트의 중심은 위에서 지시한 내용이다. 채팅 인용은 그 앞이나 뒤에 짧게 붙이고, "
                     "중심 내용을 줄이지 않는다. 위의 문장 수 제한에 채팅 인용은 넣지 않는다.")
# 곡 소개 뒤에는 곡이 바로 나간다. 채팅이 한 건일 때는 인용 → 곡 소개 순서로 쓰는데(평가 90건 중 83건),
# 두 건이 되면 곡을 먼저 소개하고 채팅을 읽는다(36건 중 35건, 2026-11-12 평가) — 순서를 정해 준다
TRACK_LAST_RULE = "채팅 인용을 모두 마친 뒤에 곡을 소개하고, 곡 소개로 멘트를 끝낸다. 이 멘트 바로 뒤에 곡이 나간다."
# 몰린 입력은 개인이 아니라 집계로 인정한다 — 특정 채팅을 인용하거나 건수를 지어내지 않는다 (9.2절)
AGGREGATE_RULE = ("멘트 첫머리에 지금 채팅창에서 아래 화제 얘기가 많이 오간다고 한 문장으로 언급한다. "
                  "특정 채팅을 인용하거나 숫자를 말하지 않는다.")
PREVIEW_RULE = ("멘트 끝에 사연 문자가 많이 와 있으니 이따 '{title}' 시간에 읽겠다고 "
                "한 문장으로 알린다. 숫자를 말하지 않는다.")
# 다시 꺼내기 — 이미 방송에 나간 청취자 글이다. 닉네임은 블록에 있는 것만 쓴다(채팅은 넣지 않는다)
# 지금 화제와 억지로 잇게 하면 어색하다 — 2026-11-15 실제 방송에서 다시 꺼낸 말이 화제와 맞지 않았다
CALLBACK_RULE = ("아래 앞서 방송에서 소개한 청취자 이야기를 '아까 ~라고 해 주셨죠'처럼 짧게 다시 꺼낸다. "
                 "지금 화제와 닿는 데가 있으면 자연스럽게 잇고, 닿지 않으면 억지로 연결하지 않고 그 이야기가 "
                 "다시 생각났다고 꺼내 한두 마디 덧붙인다. "
                 "내용을 지어내거나 뜻을 바꾸지 않고, 닉네임은 블록에 있을 때만 부른다.")
# 시간 맥락은 화제를 꺼내는 단계에만 붙인다 — 매 멘트에 붙이면 "벌써 자정이네요"가 되풀이된다
TIME_RULE = "아래 지금 시각은 이야기와 어울릴 때만 짧게 쓰고, 꼭 말하지 않아도 된다."
# 질문은 곡 앞에서 던지고 답은 곡 뒤에 받는다 (CORNER_DESIGN 7.7.2절). 이야기 도중에 물으면 답이 오는 30~60초 동안
# DJ는 다른 이야기를 하고 있어, 물어 놓고 넘어간 것처럼 들린다. 곡이 그 시간을 가려 준다
# 말끝 예시("~곡 듣고 올게요")는 넣지 않는다 — 넣으면 그 말끝이 그대로 되풀이된다 (T-007)
ASK_MARKER = "곡을 소개하기 전에"  # 더미 LLM이 곡 앞 질문을 알아보는 구절 (pipeline/llm.py)
# "아래 지금 화제를 벗어나지 않는, 한마디로 답할 수 있는 질문"이라고만 했더니, 곡마다 "겨울 간식 중에 지금 가장 당기는
# 건 뭔가요?" 같은 넓은 질문이 나오고 "한마디로 답해 주시면 돼요. 답은 곡이 나가는 동안 채팅으로 남겨 주세요"처럼
# 지시문의 글귀가 그대로 읽혔다(시험 실행 20261119-130733). 화제 블록을 뺐을 때는 "식사 뒤에 물을 챙기시나요?"처럼
# 주제 밖의 것을 물었다(20261119-133632). 그래서 화제 안에서, 직전 이야기의 구체적인 내용으로 묻게 한다
ASK_RULE = (f"이번에는 {ASK_MARKER}, 청취자에게 가벼운 질문을 하나 던진다. 질문은 아래 지금 화제 안에서, 직전 멘트에서 "
            "방금 한 이야기의 구체적인 내용으로 고른다 — 화제 전체를 묻는 넓은 질문은 하지 않는다. 길게 쓰지 않아도 답할 수 있는 "
            "질문으로 하고, 질문 문장은 물음표로 끝낸다. 이어서, 곡을 듣는 동안 답을 채팅으로 보내 달라는 것과 곡이 끝나면 읽겠다는 것을 "
            "DJ의 말투로 한 문장에 담는다 — 이 지시문의 글귀를 그대로 옮기지 않는다. 그 뒤에 곡을 소개하며 "
            "멘트를 끝낸다. 질문과 안내는 위의 문장 수 제한에 넣지 않는다.")
ASK_AFTER_QUOTE_RULE = "채팅 인용을 먼저 마치고 그 뒤에 묻는다."
ASK_NEW_RULE = "아래 이미 던진 질문과 겹치지 않는 것을 묻는다."
# 되짚지 않고 답부터 말하면 불쑥 시작한 것처럼 들린다("손끝이 먼저 따뜻해지는 편이에요.") — 첫 문장을 정해 준다
ASKED_RULE = ("곡이 나가기 전에 DJ는 아래 질문을 청취자에게 던지고, 곡이 끝나면 답을 읽겠다고 했다. "
              "{where} 곡 전에 무엇을 물었는지 한마디로 되짚는 문장으로 시작한다. 질문을 그대로 되풀이하지는 않는다. "
              "질문으로 돌아오는 말은 위의 문장 수 제한에 넣지 않는다.")
# "아직 답은 없네요"라고 말한 적이 있다 — 채팅은 왔는데 답인 글이 없을 때였다 (시험 실행 20261119-133632)
ASKED_REPLIES_RULE = ("아래 채팅 가운데 그 질문의 답인 것은 질문에 온 답으로 받아 소개한다. "
                      "받지 않은 답을 지어내지 않는다. 답인 글이 하나도 없으면 답을 읽겠다거나 답이 없다는 말은 하지 않고 "
                      "DJ 자신의 답을 한 문장으로 말한다. 답을 받는 말에 다른 이야기를 섞지 않는다.")
# 질문 뒤에 온 글을 후보로 넉넉히 주고 답인 것만 고르게 한다. 고른 대로 다 받게 했을 때는 "답을 읽어 볼게요" 하고
# "가방 챙기기 귀찮다"는 글을 읽었다 (본 실행 20261119-143036). 받지 않은 글은 되돌아가 다음 멘트가 받는다
_ANSWER_QUOTE_RULE = ("아래 청취자 채팅은 질문을 던진 뒤에 온 글이다. 이 가운데 질문의 답인 글만 골라 세 건까지 받는다. "
                      "한 건씩 '{said}'처럼 청취자의 말임을 밝혀 짧게 인용하고 한마디로 반응한다. "
                      "답이 아닌 글은 이 멘트에서 받지 않는다 — 다음 멘트가 따로 받는다. "
                      "문장 끝은 DJ의 말투로 맺는다. 닉네임이나 아이디는 말하지 않는다.")
ANSWER_QUOTE_RULE = _ANSWER_QUOTE_RULE.format(said=SAID_FORM)
ASKED_ALONE_RULE = ("받은 답은 아직 없다. 답이 없다는 말은 하지 않고, DJ 자신의 답을 한두 문장으로 말한 뒤 "
                    "이야기를 잇는다. 청취자의 답을 지어내지 않는다.")
# 답은 곡 뒤 첫 멘트가 다 받지 못한다(한 멘트에 인용 한두 건) — 뒤 멘트가 받는 답도 질문과 함께 받는다
ASKED_EARLIER_RULE = ("아래 질문은 DJ가 곡 전에 청취자에게 던진 것이다. 아래 채팅이 그 질문의 답이면 "
                      "질문에 온 답으로 받아 소개한다. 질문을 처음부터 다시 읽지는 않는다.")
# 시간대는 모든 멘트에 알린다 — 화제를 꺼내는 단계에만 시각을 넣었더니 그 밖의 멘트가 새벽 4시에 "오늘 저녁은
# 천천히 접어 두세요"라고 했다(방송 40개 중 9개, T-016). 시각을 말하라는 지시가 아니라 어긋나지 말라는 지시다
# 요일과 시각을 앞세워 "지금은 목요일 오후 1시쯤이다"라고 했더니 "목요일 오후, 잠시 마음을…", "오후의 길 위를…"처럼
# 시간대를 말하는 멘트가 한 방송에 여섯 번 나왔다(시험 실행 20261119-130733). 일부러 넣지 말라고 먼저 말한다
# "방송 시간대는 새벽이다"라고 쓰면 안 된다 — DJ 이름이 '새벽'인 방송 40개 가운데 7개가 오프닝에서 "새벽다입니다"라고
# 했다(시스템 프롬프트의 "DJ 새벽다."와 엉켰다, 본 실행 20261119-143036). 시간대 낱말에 서술격 조사를 붙이지 않는다
DAYTIME_RULE = ("방송 시간대: {part} {clock}. 이것을 멘트에 일부러 넣지 않는다. 때를 가리키는 말을 쓸 일이 "
                "있을 때만 이 시간대에 맞게 쓰고, 지금을 {others}처럼 말하지 않는다.")
# 답이 없는 방송에서 계속 묻지 않는다 — 조용한 방송의 이야기 멘트 75%가 질문으로 끝났다 (2026-11-11 기준선, #262)
NO_QUESTION_RULE = "청취자에게 질문하지 않는다. 묻는 대신 DJ가 생각을 풀어 가고, 질문으로 끝내지 않는다."
# 곡 뒤에 고정 문구("다시 …로 돌아왔어요")를 틀지 않는다 — 그 자리는 이 멘트가 잇는다
AFTER_SONG_RULE = "곡이 끝나고 다시 말을 잇는 자리다. 새로 인사하거나 돌아왔다고 말하지 않는다."
# 곡이 끝나면 방금 나간 곡이 무엇이었는지부터 알린다 (사용자 결정 2026-11-20, T-020). 예전에는 한 번 걸러 한 번
# "한마디로 받고" 넘어가게 했는데, 곡 뒤 첫 멘트 100건 가운데 제목을 말한 것은 5건이고 나머지는 "방금 곡의 경쾌함이…"
# 처럼 느낌만 말하거나 곡을 건드리지 않고 다음 이야기로 넘어갔다 (수집 실행 20261120-041934)
# 말끝 예시("지금 들으신 곡은 ~입니다")는 넣지 않는다 — 넣으면 그 말끝이 persona와 상관없이 되풀이된다 (T-007)
BACK_ANNOUNCE_RULE = ("{first} 방금 나간 곡이 무엇이었는지 알려 주는 말이다 — 아래 방금 나간 곡의 제목과 가수를 넣어 "
                      "DJ의 말투로 짧게 말한다. 곡을 다시 소개하거나 감상을 길게 풀지 않고, 그다음 문장부터 이야기로 "
                      "넘어간다. 곡을 알려 주는 문장은 위의 문장 수 제한에 넣지 않는다.")
BACK_ANNOUNCE_THEN_RULE = "청취자의 채팅이나 반응을 받을 것이 있으면 곡을 알려 준 바로 다음에 받고, 그 뒤에 이야기로 넘어간다."
ANGLE_CONTINUE_RULE = "아래 이번 갈래는 앞에서 하던 이야기다. 처음 꺼내듯 말하지 않고 하던 이야기를 이어 간다."
ANGLE_HANDOFF_RULE = ("아래 다룬 갈래 가운데 마지막 것이 바로 앞에서 한 이야기다. "
                      "그 이야기에서 자연스럽게 넘어오며 이번 갈래를 꺼낸다.")
# 갈래에 '하려는 말'이 붙어 있을 때의 지시 (Angle.gist, inputs/topic_plan.py, T-022)
GIST_BLOCK_RULE = ("이 덩어리 전체가 아래 이번 갈래의 '하려는 말' 하나를 향한다. 말할 거리는 그 말을 풀어 가는 재료이고, "
                   "다 쓰지 않아도 된다. 문단이 바뀌며 새 이야깃거리를 꺼낼 때는 그것이 앞 문단의 이야기와 어떻게 "
                   "이어지는지를 말로 잇는다. 마지막 문단에서는 하려는 말을 DJ 자신의 생각으로 분명하게 말한다. "
                   "빛·소리·향기 같은 분위기 묘사와 위로하는 말은 덩어리 전체에서 한두 문장을 넘기지 않는다.")
GIST_TALK_RULE = ("말할 거리는 아래 '하려는 말'을 풀어 가는 재료다. 분위기 묘사나 위로하는 말만으로 멘트를 채우지 않는다.")
DJ_OPINION_RULE = "DJ 자신의 취향이나 의견을 말해도 된다."
# 고른 사람들이 가장 자주 적은 이유가 꾸밈말이었다 — "거래의 첫 박자", "식사의 마침표 같아", "각자의 고요로 돌아가는 불빛".
# 예로 든 말은 프롬프트에 넣지 않는다(넣으면 그 낱말이 나온다, T-007)
PLAIN_WORDS_RULE = ("사물이나 상황을 다른 것에 빗대는 비유와 시처럼 꾸민 표현은 쓰지 않는다. "
                    "아는 사람에게 말하듯 평범한 낱말로 말한다.")
# 이야기 덩어리 — 곡 사이 말을 한 번에 쓴다. 문단 하나가 세그먼트 하나로 합성된다 (pipeline._write_block)
BLOCK_STEP = "talk_block"
BLOCK_MARKER = "이야기 한 덩어리"
PREV_TRACK = "방금 나간 곡"


def track_line(track: dict) -> str:
    line = f"곡: {track['title']} — {track.get('artist') or '알 수 없는 아티스트'}"
    return line + (f" (무드: {track['mood']})" if track.get("mood") else "")


def news_lines(items: list[dict]) -> str:
    # URL은 소리 내어 읽을 수 없고 L2 룰(URL 금지)에 걸린다 — 매체 이름만 넘긴다
    return "\n".join(f"- {it['title']}: {it['summary']} (출처: {it['source']})" for it in items)


def _with_topic(text: str, m: Material) -> str:
    return text + ("\n" + data_block(TOPIC, m.topic) if m.topic else "")


def _opening_greeting(m: Material) -> str:
    # 첫 곡은 백엔드가 방송 시작과 함께 이미 틀었다 — track(music_ref)이 아니라 extra로 받는다
    if first := m.extra.get("first_song"):
        return ("방송을 시작한다. 인사하고, 방금 흘러나온 첫 곡을 한마디로 언급하는 오프닝 멘트를 "
                "3문장 이내로 작성하라.\n" + data_block(TRACK, track_line(first)))
    return "방송을 시작한다. 스테이션 컨셉에 맞는 오프닝 멘트를 3문장 이내로 작성하라."


def _topic_intro(m: Material) -> str:
    return _with_topic("오늘의 주제를 소개하는 멘트를 2문장 이내로 작성하라. 주제는 아래 데이터 블록에 있다.", m)


def _corner_preview(m: Material) -> str:
    guide = m.extra.get("guide")
    text = "오늘 이어질 코너를 예고하는 멘트를 3문장 이내로 작성하라. "
    text += (f"참여 방법도 안내하되 이 내용을 빠뜨리지 않는다: {guide}" if guide
             else "청취자 참여 방법은 말하지 않는다.")
    titles = "\n".join(f"- {t}" for t in m.extra.get("corners", []))
    return text + ("\n" + data_block(CORNERS, titles) if titles else "")


def _angle(m: Material) -> str:
    """이번 갈래(inputs/topic_plan.py) 블록. 갈래 계획이 없거나 채팅이 화제를 바꿨으면 비어 있다."""
    angle = m.extra.get("angle")
    return "\n" + data_block(ANGLE, angle.text()) if angle else ""


def _question(m: Material) -> str:
    if m.extra.get("question"):
        return " 끝에 청취자에게 가볍게 질문을 하나 던진다."
    return f" {NO_QUESTION_RULE}" if m.extra.get("no_question") else ""


def announces_song(m: Material) -> bool:
    """이 멘트가 방금 나간 곡을 알려 주는가 — 곡 전에 던진 질문으로 돌아오는 멘트는 답만 받는다 (7.7.2절, 사용자 결정)."""
    return bool(m.extra.get("after_song") and m.extra.get("back_announce") and not m.extra.get("asked"))


def _after_song(m: Material, first: str = "첫 문장은") -> tuple[str, str]:
    """곡 뒤에 말을 잇는 자리의 지시와 데이터 블록. 곡 뒤가 아니면 둘 다 빈 문자열."""
    track = m.extra.get("after_song")
    if not track:
        return "", ""
    if announces_song(m):
        rule = f" {AFTER_SONG_RULE} {BACK_ANNOUNCE_RULE.format(first=first)}"
        if m.quotes or m.mood:
            rule += f" {BACK_ANNOUNCE_THEN_RULE}"
        return rule, "\n" + data_block(PREV_TRACK, track_line(track))
    return f" {AFTER_SONG_RULE}", ""


def _gist(m: Material, rule: str) -> str:
    """갈래에 하려는 말이 붙어 있으면 그 지시, 아니면 빈 문자열 (설정 talk_gist)."""
    angle = m.extra.get("angle")
    level = getattr(angle, "level", 0)
    if not level or not angle.gist:
        return ""
    return f" {rule} {DJ_OPINION_RULE}" + (f" {PLAIN_WORDS_RULE}" if level >= 2 else "")


def _angle_rule(m: Material, fresh: str) -> str:
    """이번 갈래를 어떻게 꺼낼지 — 하던 갈래면 잇고, 새 갈래면 앞 갈래에서 넘어온다."""
    if not m.extra.get("angle"):
        return ""
    if m.extra.get("angle_state") == "continue":
        return f" {ANGLE_CONTINUE_RULE}"
    if m.extra.get("covered_angles"):
        return f" {ANGLE_HANDOFF_RULE}"
    return fresh


def _opener_context(m: Material) -> str:
    """화제를 꺼내는 단계의 덧붙임 — 질문, 이미 다룬 갈래, 지금 시각."""
    text = _question(m)
    if covered := m.extra.get("covered_angles"):
        text += (" 아래 이미 다룬 갈래는 되풀이하지 않는다.\n"
                 + data_block(COVERED_ANGLES, "\n".join(f"- {t}" for t in covered)))
    if now := m.extra.get("now"):
        text += f"\n{TIME_RULE}\n" + data_block(NOW, now)
    return text


def _talk_open(m: Material) -> str:
    text = "오늘의 주제로 이야기를 꺼내는 멘트를 3문장 이내로 작성하라."
    # 오프닝(topic_intro)이 주제를 이미 소개했다 — 2026-11-07 실행에서 바로 뒤에 한 번 더 소개했다
    text += _angle_rule(m, " 주제는 오프닝에서 이미 소개했으니 다시 소개하지 않고, 아래 이번 갈래로 바로 시작한다.")
    rule, block = _after_song(m)
    return _with_topic(text + rule + _opener_context(m), m) + _angle(m) + block


def _talk_resume(m: Material) -> str:
    # 다른 말 코너를 거쳐 왔으면 직전에 접착 문구(resume)가 "다시 … 돌아왔어요"를 이미 말했다
    text = ("다른 순서를 지나 다시 오늘의 이야기로 돌아온 멘트를 3문장 이내로 작성하라. "
            f"주제를 처음 소개하듯 말하지 않고, 돌아왔다는 말도 하지 않는다. {NO_RESTATE_RULE}")
    text += _angle_rule(m, " 아래 이번 갈래로 화제를 옮긴다.") or " 앞에서 한 말과 다른 면을 꺼낸다."
    rule, block = _after_song(m)
    return _with_topic(text + rule + _opener_context(m), m) + _angle(m) + block


def _talk(m: Material) -> str:
    # 소재 하나로 한 멘트 — 편성 관리자가 채팅·다시 꺼낼 이야기·갈래의 말할 거리 중 하나를 골라 넣는다 (7.7절)
    text = ("지금 화제로 이야기를 이어가는 멘트를 3문장 이내로 작성하라. 직전 멘트와 같은 말을 되풀이하지 않는다. "
            f"{NO_RESTATE_RULE}")
    text += f" {NO_QUESTION_RULE}" if m.extra.get("no_question") else " 질문으로 끝내지 않아도 된다."
    if m.extra.get("steered"):
        text += f" {STEERED_RULE}"
    if callback := m.extra.get("callback"):
        return _with_topic(f"{text} {CALLBACK_RULE}", m) + "\n" + data_block(CALLBACK, callback.text())
    if m.extra.get("angle"):
        text += " 아래 이번 갈래의 말할 거리로 이야기한다." + _gist(m, GIST_TALK_RULE)
    return _with_topic(text, m) + _angle(m)


def _talk_block(m: Material) -> str:
    """곡과 곡 사이 말 전체를 한 번에 — 채팅이 없는 동안의 DJ 주도 진행 (CORNER_DESIGN 7.7.1절)."""
    parts = int(m.extra.get("parts", 3))
    text = (f"곡과 곡 사이에 DJ가 혼자 이어 말할 {BLOCK_MARKER}를 쓴다. 문단 {parts}개로 나누고, 문단마다 2~3문장을 쓰고, "
            "문단 사이는 빈 줄로 띄운다. 한 사람이 한 호흡으로 말하듯 문단이 앞 문단을 받아 이어져야 한다. "
            "문단마다 새로 인사하거나 주제를 다시 소개하지 않고, 같은 말을 되풀이하지 않는다.")
    rule, blocks = _after_song(m, "첫 문단의 첫 문장은")
    text += rule
    if song := m.extra.get("song_story"):
        # 주제 이야기만 되풀이하지 않게 가끔 방금 나간 곡 이야기로 쉬어 간다. 다음 곡 이야기는 할 수 없다 —
        # 이 덩어리를 쓰는 시점에는 백엔드가 다음 곡을 아직 알려 주지 않는다 (ENGINE_TUNING_LOG T-008)
        text += (" 이번에는 주제 이야기를 잠시 쉬고, 아래 방금 나간 곡을 듣고 떠오른 이야기를 한다 — "
                 "곡의 분위기, 그 곡이 어울리는 시간이나 장면 같은 것이다. "
                 "곡을 다시 소개하거나 다시 틀겠다고 하지 않는다. "
                 "수상·발매 시기·일화처럼 확인할 수 없는 사실은 말하지 않는다. "
                 "오늘 주제와 닿는 데가 있으면 가볍게 잇는다.")
        if m.extra.get("plain_words"):
            text += f" {PLAIN_WORDS_RULE}"
        if PREV_TRACK not in blocks:  # 곡을 알려 주는 지시가 이미 곡을 넣었다
            blocks += "\n" + data_block(PREV_TRACK, track_line(song))
    elif m.extra.get("angle"):
        text += _angle_rule(m, " 주제는 이미 소개했으니 다시 소개하지 않고, 아래 이번 갈래로 바로 시작한다.")
        text += _gist(m, GIST_BLOCK_RULE) or " 이번 갈래의 말할 거리를 문단에 차례로 풀어 쓴다."
    else:
        text += " 오늘 주제에서 아직 하지 않은 말을 풀어 간다."
    if callback := m.extra.get("callback"):
        text += f" 문단 하나에서는 {CALLBACK_RULE}"
        blocks += "\n" + data_block(CALLBACK, callback.text())
    if m.extra.get("question"):
        text += " 마지막 문단 끝에만 청취자에게 가볍게 질문을 하나 던진다. 다른 문단에서는 묻지 않는다."
    else:
        text += f" {NO_QUESTION_RULE}"
    if covered := m.extra.get("covered_angles"):
        text += " 아래 이미 다룬 갈래는 되풀이하지 않는다."
        blocks += "\n" + data_block(COVERED_ANGLES, "\n".join(f"- {t}" for t in covered))
    if now := m.extra.get("now"):
        text += f" {TIME_RULE}"
        blocks += "\n" + data_block(NOW, now)
    return _with_topic(text, m) + ("" if m.extra.get("song_story") else _angle(m)) + blocks


_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_SENTENCE_RE = re.compile(r"(?<=[.?!…])\s+")


def split_block(raw: str, parts: int, *, keep_vocal: bool = False) -> list[str]:
    """덩어리 대본을 문단으로 — 문단 하나가 세그먼트 하나다. 거부면 [REJECT], 쓸 문단이 없으면 [].

    keep_vocal이면 허용한 발성 태그를 문단에 남긴다 (pipeline.llm.clean_script).

    LLM이 빈 줄 없이 한 문단으로 쓰면 문장 수로 고르게 나눈다. 계획보다 많이 나온 문단은 마지막 문단에 붙인다.
    """
    # 순환 import 회피 — llm이 이 모듈을 쓴다
    from ..pipeline.llm import REJECT, LlmError, clean_script

    def clean(text: str) -> str | None:
        try:
            return clean_script(text, keep_vocal=keep_vocal)
        except LlmError:  # 기호뿐인 문단
            return None

    whole = clean(raw)
    if whole is None:
        return []
    if whole == REJECT:
        return [REJECT]
    texts = [t for t in (clean(p) for p in _PARAGRAPH_RE.split(raw.strip())) if t and t != REJECT]
    if len(texts) < 2 and parts > 1:
        sentences = [s for s in _SENTENCE_RE.split(whole) if s]
        size = max(2, -(-len(sentences) // parts))
        texts = [" ".join(sentences[i:i + size]) for i in range(0, len(sentences), size)]
    if len(texts) > parts:
        texts = [*texts[:parts - 1], " ".join(texts[parts - 1:])]
    return texts


def _artist_talk(m: Material) -> str:
    return ("다음에 나갈 곡의 아티스트나 장르에 대해 이야기하는 멘트를 3문장 이내로 작성하라. "
            "곡 제목은 아직 말하지 않는다.\n" + data_block(TRACK, track_line(m.items[0])))


def _music_intro(m: Material) -> str:
    assert m.track is not None
    # 곡 제목·아티스트는 외부 텍스트다 (YouTube 메타데이터, F-34)
    # 문장처럼 읽히는 제목은 말에 녹여 버린다 — "겨울을 걷는다"를 "윤딴딴의 겨울을 걷습니다"로 소개했다 (2026-11-15, #295)
    text = ("다음 곡을 소개하는 멘트를 2문장 이내로 작성하라. 곡 제목은 적힌 그대로 한 글자도 바꾸지 않고 부른다 — "
            "제목이 문장처럼 보여도 말끝을 바꾸거나 풀어 말하지 않는다.")
    if nickname := m.track.get("requester_nickname"):
        text += " 청취자의 신청곡임을 닉네임과 함께 소개한다."
        info = track_line(m.track) + "\n신청자: " + nickname
        if comment := m.track.get("requester_comment"):
            text += " 신청자가 남긴 한마디도 언급한다."
            info += "\n한마디: " + comment
        return text + "\n" + data_block(TRACK, info)
    blocks = data_block(TRACK, track_line(m.track))
    if m.track.get("dj_pick"):  # DJ가 고른 곡 (계약 3.4절 track.suggest, #295)
        text += " DJ가 이 방송을 위해 직접 고른 곡이다."
        if why := m.track.get("pick_why"):
            # 곡 정보는 백엔드가 찾은 실제 곡이고, 까닭은 고를 때 적어 둔 것이다 — 곡이 달라졌을 수 있어 사실로 쓰지 않는다
            text += " 아래 고른 까닭을 참고해 왜 골랐는지 한마디 곁들인다. 까닭에 없는 사실을 지어내지 않는다."
            blocks += "\n" + data_block(PICK_WHY, why)
    if m.extra.get("ask"):  # 곡 앞에서 묻고 곡 뒤에 답을 받는다 (7.7.2절)
        text += f" {ASK_RULE}"
        if m.quotes:
            text += f" {ASK_AFTER_QUOTE_RULE}"
        if m.topic:
            blocks += "\n" + data_block(TOPIC, m.topic)
        if before := m.extra.get("asked_before"):
            text += f" {ASK_NEW_RULE}"
            blocks += "\n" + data_block(ASKED_BEFORE, "\n".join(f"- {q}" for q in before))
    return text + "\n" + blocks


def _story(m: Material) -> str:
    story = m.items[0]
    return ("청취자 사연을 읽고 DJ의 이야기를 덧붙이는 멘트를 4문장 이내로 작성하라. "
            "사연은 보낸 분의 닉네임과 함께 소개한다.\n"
            + data_block(STORY, f"닉네임: {story.get('nickname', '익명')}\n{story['body']}"))


def _text_read(m: Material) -> str:
    msg = m.items[0]
    # 낭독이다 — 풀어 말하면 인용 검사(pipeline.quoted)에 걸려 되돌아간다 (CORNER_DESIGN 5.3절)
    return ("청취자가 보낸 짧은 문자를 닉네임과 함께 소개한다. 문자 내용은 고치거나 요약하지 말고 "
            "그대로 읽은 뒤, 한두 문장으로 리액션하라.\n"
            + data_block(STORY, f"닉네임: {msg.get('nickname', '익명')}\n{msg['body']}"))


# DJ 대 청취자 코너 (CORNER_DESIGN 7.6절, #279) — 항목은 라운드의 지금 상태다 (inputs/votes.py view)
# DJ의 선택은 결과를 공개하는 멘트에서만 밝힌다. 그 전에 새면 청취자가 따라 고른다
HIDE_PICK_RULE = "DJ가 어느 쪽을 골랐는지는 아직 밝히지 않고, 짐작할 수 있는 말도 하지 않는다."
_OUTCOME_RULES = {
    "match": "DJ의 선택과 청취자 다수의 선택이 같았다. DJ가 맞혔다고 말한다.",
    "mismatch": "DJ의 선택과 청취자 다수의 선택이 갈렸다. 이번에는 청취자가 이겼다고 말한다.",
    "tie": "표가 똑같이 갈렸다. 무승부라고 말한다.",
    "no_votes": "표가 들어오지 않았다. 표 수는 말하지 않고, 이번 고민은 무승부로 넘어간다고 말한다.",
    "late": "집계가 늦어져 결과를 전하지 못한다. 결과를 지어내지 않고 DJ의 선택만 밝히며, 점수는 말하지 않는다.",
}


def _dilemma_block(vote: dict) -> str:
    options = " / ".join(vote.get("options", []))
    return data_block(DILEMMA, f"{vote.get('question', '')}\n선택지: {options}")


def _game_ask(m: Material) -> str:
    vote = m.items[0]
    return ("DJ와 청취자가 같은 쪽을 고르는지 겨루는 코너다. 아래 고민과 두 선택지를 소개하고 청취자에게 "
            "투표를 권하는 멘트를 3문장 이내로 작성하라. 두 선택지는 적힌 말 그대로 부르고, 투표는 화면에서 "
            f"할 수 있다고 알린다. {HIDE_PICK_RULE}\n" + _dilemma_block(vote))


def _game_talk(m: Material) -> str:
    vote = m.items[0]
    options = vote.get("options") or ["", ""]
    turn = int(vote.get("talks", 1))
    phase = vote.get("phase", "open")
    if phase == "closing":  # 이 멘트가 나갈 즈음 창이 닫힌다 — 권하지 않고 마감을 알린다
        return ("청취자 투표를 기다리며 이야기를 잇는 자리다. 이제 투표를 마감한다고 알리고, 결과를 확인하는 동안 "
                f"잠깐 기다려 달라고 말하는 멘트를 2문장 이내로 작성하라. 투표를 더 권하지 않는다. {HIDE_PICK_RULE} "
                "투표 결과를 짐작하거나 지어내지 않는다.\n" + _dilemma_block(vote))
    if phase == "closed":
        # 창은 닫혔고 결과 공개 멘트를 만드는 중이다. 결과가 청취자에게 들리기까지 25초쯤 걸린다(공개 멘트 생성 +
        # 방송 지연). "결과가 곧 나온다"고만 시켰더니 그 말이 서너 번 되풀이됐다 (2026-11-14 실제 LLM 10회)
        return ("청취자 투표를 기다리며 이야기를 잇는 자리다. 투표는 이미 마감됐다. 결과를 확인하는 동안, 아래 고민을 "
                "두고 떠오르는 장면 하나를 가볍게 이야기하는 멘트를 3문장 이내로 작성하라. 어느 한쪽 편을 들지 않고, "
                f"투표를 더 권하지 않는다. {HIDE_PICK_RULE} 결과가 곧 나온다는 말은 하지 않아도 된다. "
                "투표 결과를 짐작하거나 지어내지 않고, 직전 멘트와 같은 말을 되풀이하지 않는다.\n" + _dilemma_block(vote))
    text = "청취자 투표를 기다리며 이야기를 잇는 멘트를 3문장 이내로 작성하라. "
    if turn <= 2:  # 두 쪽을 한 번씩 편든다 — 한쪽만 말하면 DJ의 선택처럼 들린다
        text += f"아래 고민에서 '{options[(turn - 1) % 2]}' 쪽을 고르는 마음을 풀어 말한다. "
    elif turn % 2:  # 결과가 올 때까지 잇는다 — 권하는 말만 되풀이하지 않게 번갈아 한다
        text += "아직 고르지 못한 청취자에게 가볍게 투표를 권하고, 두 선택지를 다시 한 번 부른다. "
    else:
        text += ("아래 고민을 두고 흔히 망설이게 되는 순간을 가볍게 이야기한다. 어느 한쪽 편을 들지 않고, "
                 "두 선택지를 한 번씩 다시 부른다. ")
    text += (f"{HIDE_PICK_RULE} 투표 결과를 짐작하거나 지어내지 않고, 직전 멘트와 같은 말을 되풀이하지 않는다.\n")
    return text + _dilemma_block(vote)


def _game_reveal(m: Material) -> str:
    vote = m.items[0]
    options = vote.get("options") or ["", ""]
    outcome = vote.get("outcome", "late")
    text = ("DJ의 선택과 청취자 투표 결과를 공개하는 멘트를 6문장 이내로 짧게 작성하라. 먼저 DJ가 "
            f"'{options[int(vote.get('pick', 0))]}' 쪽을 골랐다고 밝히고 아래 까닭을 DJ의 말투로 한 문장으로 말한다. ")
    if outcome in ("match", "mismatch", "tie"):
        text += "이어서 아래 투표 결과의 표 수를 적힌 숫자 그대로 전한다. 숫자를 바꾸거나 지어내지 않는다. "
    text += _OUTCOME_RULES[outcome]
    if outcome != "late" and (score := vote.get("score")):
        text += f" 끝으로 지금 점수가 DJ {score['dj']}점, 청취자 {score['listeners']}점이라고 알린다."
    blocks = [data_block(DILEMMA, f"{vote.get('question', '')}\nDJ가 고른 까닭: {vote.get('reason', '')}")]
    if outcome in ("match", "mismatch", "tie"):
        first, second = vote.get("tally", (0, 0))
        blocks.append(data_block(VOTE_RESULT, f"{options[0]}: {first}표\n{options[1]}: {second}표"))
    return text + "\n" + "\n".join(blocks)


def _game_final(m: Material) -> str:
    score = m.extra.get("score") or {"dj": 0, "listeners": 0, "draw": 0}
    dj, listeners = score["dj"], score["listeners"]
    winner = ("DJ가 이겼다" if dj > listeners else "청취자가 이겼다" if listeners > dj else "비겼다")
    return (f"코너를 마치며 최종 점수를 알리는 멘트를 2문장 이내로 작성하라. DJ {dj}점, 청취자 {listeners}점으로 "
            f"{winner}고 숫자 그대로 말하고, 함께해 준 청취자에게 짧게 인사한다. 새 고민을 꺼내지 않는다.")


def _news_headline(m: Material) -> str:
    return ("다음 소식들의 헤드라인을 한 문장씩 전하라. 소재에 없는 사실은 말하지 않고, "
            "출처는 매체 이름으로 밝힌다.\n" + data_block(SOURCES, news_lines(m.items)))


def _news_item(m: Material) -> str:
    field = f"{m.label} 분야의 " if m.label else ""
    return (f"다음 {field}기사 하나를 두세 문장으로 요약해 전하라. 소재에 없는 사실은 말하지 않고, "
            "출처는 매체 이름으로 밝힌다.\n" + data_block(SOURCES, news_lines(m.items)))


def _news_recap(m: Material) -> str:
    titles = "\n".join(f"- {t}" for t in m.extra.get("covered", []))
    text = "이번에 전한 소식을 두 문장 이내로 정리하라. 새로운 사실은 덧붙이지 않는다."
    return text + ("\n" + data_block(SOURCES, titles) if titles else "")


def _closing_lookback(m: Material) -> str:
    text = "방송을 마무리하며 오늘 방송을 짧게 돌아보는 멘트를 3문장 이내로 작성하라."
    if aired := m.extra.get("lookback"):
        text += (" 아래 오늘 방송에 나온 청취자 이야기 중 한두 가지를 짧게 짚는다. 내용을 지어내거나 뜻을 바꾸지 않고, "
                 "닉네임은 블록에 있을 때만 부른다.\n"
                 + "\n".join(data_block(CALLBACK, a.text()) for a in aired))
    if covered := m.extra.get("covered_angles"):
        text += "\n아래는 오늘 나눈 이야기 갈래다. 돌아볼 때 참고만 하고 목록처럼 읽지 않는다.\n" + data_block(
            COVERED_ANGLES, "\n".join(f"- {t}" for t in covered))
    return text


def _closing_farewell(m: Material) -> str:
    text = "마지막 곡을 소개하고 끝인사를 하는 멘트를 3문장 이내로 작성하라."
    if not m.track:
        return text
    info = track_line(m.track)
    # 엔딩 곡이 된 신청곡은 신청자 없이 소개됐다 — 곡 소개로 나간 신청곡에는 모두 닉네임이 있었다 (O-008)
    if nickname := m.track.get("requester_nickname"):
        text += " 마지막 곡이 청취자의 신청곡임을 닉네임과 함께 소개한다."
        info += "\n신청자: " + nickname
        if comment := m.track.get("requester_comment"):
            text += " 신청자가 남긴 한마디도 언급한다."
            info += "\n한마디: " + comment
    return text + "\n" + data_block(TRACK, info)


def _filler(m: Material) -> str:
    return _with_topic("곡 사이를 잇는 짧은 브릿지 멘트를 1~2문장으로 작성하라.", m)


@dataclass(frozen=True)
class StepKind:
    marker: str  # 지시문에 반드시 들어가는 구절 — 더미 LLM의 단계 식별 표식
    build: Callable[[Material], str]
    seconds: tuple[int, int]  # 예상 발화 길이 [예시] — 평가 도구의 길이 경고 기준, 실측 후 조정
    quotable: bool = False  # 채팅 인용을 붙일 수 있는 단계 (코너 계약이 인용을 허용할 때만 붙는다)


STEP_KINDS: dict[str, StepKind] = {
    "opening_greeting": StepKind("방송을 시작한다", _opening_greeting, (15, 40)),
    "topic_intro": StepKind("오늘의 주제를 소개", _topic_intro, (8, 25)),
    "corner_preview": StepKind("코너를 예고", _corner_preview, (10, 30), quotable=True),
    "talk_open": StepKind("이야기를 꺼내는", _talk_open, (10, 30), quotable=True),
    "talk_resume": StepKind("다시 오늘의 이야기로 돌아온", _talk_resume, (10, 30), quotable=True),
    "talk": StepKind("이야기를 이어가는", _talk, (15, 45), quotable=True),
    # 덩어리의 문단 하나가 세그먼트 하나다 — 길이 기준은 문단에 건다. 채팅은 덩어리 뒤의 이야기 단계가 인용한다
    BLOCK_STEP: StepKind(BLOCK_MARKER, _talk_block, (8, 25)),
    "artist_talk": StepKind("아티스트나 장르", _artist_talk, (10, 30), quotable=True),
    "music_intro": StepKind("다음 곡을 소개", _music_intro, (10, 25), quotable=True),
    "story": StepKind("사연을 읽고", _story, (20, 60), quotable=True),
    "text_read": StepKind("짧은 문자를", _text_read, (8, 25)),
    "game_ask": StepKind("투표를 권하는", _game_ask, (10, 30)),
    "game_talk": StepKind("투표를 기다리며", _game_talk, (10, 30), quotable=True),
    "game_reveal": StepKind("투표 결과를 공개", _game_reveal, (15, 40)),
    "game_final": StepKind("최종 점수", _game_final, (8, 20)),
    "news_headline": StepKind("헤드라인을", _news_headline, (15, 45)),
    "news_item": StepKind("기사 하나를", _news_item, (20, 60)),
    "news_recap": StepKind("전한 소식을", _news_recap, (10, 30)),
    "closing_lookback": StepKind("돌아보는", _closing_lookback, (10, 30), quotable=True),
    "closing_farewell": StepKind("끝인사", _closing_farewell, (10, 30)),
    "filler": StepKind("브릿지", _filler, (5, 15)),
}


def _mood(reactions: list, after_announce: bool = False) -> str:
    """반응 분위기 지시와 데이터 블록 — 반응들과, 알면 그때 방송에 나가던 말."""
    bodies = list(dict.fromkeys(r.body.strip() for r in reactions))[:_MOOD_MAX_LINES]
    rule = MOOD_AFTER_ANNOUNCE_RULE if after_announce else MOOD_RULE
    text = f"\n{rule}\n" + data_block(REACTIONS, "\n".join(bodies))
    heard = list(dict.fromkeys(r.heard for r in reactions if r.heard))[:2]
    if heard:
        text += f"\n{MOOD_HEARD_RULE}\n" + data_block(
            HEARD, "\n".join(h[:_HEARD_MAX_CHARS] for h in heard))
    return text


def build_prompt(step: str, material: Material, profile: StationProfile, *,
                 plain: bool = False) -> Prompt:
    kind = STEP_KINDS[step]
    user = kind.build(material)
    if asked := material.extra.get("asked"):  # 곡 전에 던진 질문으로 돌아온다 — 답이 왔으면 받고, 없으면 DJ가 답한다
        where = "첫 문단은" if step == BLOCK_STEP else "이 멘트는"
        follow = ASKED_REPLIES_RULE if material.quotes else ASKED_ALONE_RULE
        user += f"\n{ASKED_RULE.format(where=where)} {follow}\n" + data_block(ASKED, asked)
    elif material.quotes and (earlier := material.extra.get("asked_earlier")):
        user += f"\n{ASKED_EARLIER_RULE}\n" + data_block(ASKED, earlier)
    if material.quotes:
        if not kind.quotable:
            raise ValueError(f"{step} 단계는 채팅을 인용하지 않는다")
        quotes, steer = list(material.quotes), material.extra.get("steer")
        answers = bool(material.extra.get("optional_quotes"))  # 답인 글만 골라 받는다 — 다 받으라고 하지 않는다
        said, other, asked_form = material.extra.get("quote_forms") or (SAID_FORM, None, ASKED_FORM)
        rule = (_ANSWER_QUOTE_RULE.format(said=said) if answers
                else _QUOTE_RULE.format(said=said, asked=asked_form))
        if other and (answers or len(quotes) > 1):
            rule += " " + VARY_FORM_RULE.format(other=other)
        if len(quotes) > 1 and not answers:
            rule += f" {MULTI_QUOTE_RULE}"
            if steer:  # 화제가 될 채팅(첫 건)을 마지막에 인용해야 "이 얘기를 더"로 이어진다
                quotes = [*quotes[1:], quotes[0]]
        if steer:
            rule += f" {MULTI_STEER_RULE if len(quotes) > 1 else STEER_RULE}"
        if material.feature_ref:
            rule += f" {WITH_FEATURE_RULE}"
        if material.track and (len(quotes) > 1 or material.feature_ref):
            rule += f" {TRACK_LAST_RULE}"
        jokes = [i for i, q in enumerate(quotes, 1) if q.tone == TONE_JOKE]
        if jokes:
            which = "이 채팅은" if len(quotes) == 1 else ", ".join(f"{i}번째" for i in jokes) + " 채팅은"
            rule += " " + JOKE_RULE.format(which=which)
        chats = "\n".join(q.body for q in quotes)
        user += f"\n{rule}\n" + data_block(CHAT, chats)
        heard = list(dict.fromkeys(q.heard for q in quotes if q.back_ref and q.heard))[:2]
        if heard:
            user += f"\n{QUOTE_HEARD_RULE}\n" + data_block(
                HEARD, "\n".join(h[:_HEARD_MAX_CHARS] for h in heard))
    if material.mood:
        if not kind.quotable:
            raise ValueError(f"{step} 단계는 채팅 반응을 전하지 않는다")
        user += _mood(material.mood, announces_song(material))
    if trend := material.extra.get("aggregate"):
        user += f"\n{AGGREGATE_RULE}\n" + data_block(TREND, trend.keyword)
    elif preview := material.extra.get("preview"):
        user += "\n" + PREVIEW_RULE.format(title=preview["title"])
    if daytime := material.extra.get("daytime"):
        user += "\n" + DAYTIME_RULE.format(**daytime)
    return Prompt(system=system_prompt(profile, plain=plain), user=user)
