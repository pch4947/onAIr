# 백엔드 YouTube 다운로드 운영

## 구조와 범위

로그인 사용자 음원 등록 API → 별도 다운로드 프로세스 → 내부 Lavalink → 전체 MP4 파일 →
FFmpeg MP3 → 기존 Redis 방송 큐/HLS. Lavalink의 Discord 연결 기능은 사용하지 않습니다.
청취 화면에서 링크를 등록하거나 검색어로 최대 3개 후보를 비교·선택할 수 있습니다.
검색은 내부 Lavalink의 `ytsearch:`를 사용하며 다운로드·큐 등록과 별도입니다.

## 검색 후보 선택

로그인 인증 후 `POST /api/stations/{station_id}/tracks/search`에
`{"query":"가수 곡 제목"}`을 보냅니다. `candidates` 배열의 항목은
`video_id`, `url`, `title`, `channel`, `duration_ms`, `thumbnail_url`입니다.
중복·라이브·길이 불명·최대 재생시간 초과 영상은 제외하고 최대 3개를 반환합니다.
결과가 적거나 없을 수도 있으며, 검색 성공이 다운로드 가능성을 보장하지 않습니다.
선택한 항목의 `url`을 기존 음원 등록 API로 전달합니다. 백엔드에서 길이를 다시 검증합니다.

`application.yml`의 `plugins.youtube.allowSearch: true`가 필요합니다.
false이면 유명한 곡도 빈 결과가 나올 수 있습니다. 수정 후 Lavalink를 재시작합니다.
외부 포트나 OAuth 권한 추가는 필요 없습니다. 검색은 최대 10초, 프론트는 15초 제한입니다.
검색어 변경 시 이전 결과·선택을 초기화하며 이전 요청을 취소합니다.
미리듣기·일반 청취자 신청·곡 목록 UI는 포함하지 않습니다.

검증 버전은 Lavalink 4.2.2와 youtube-source 커밋
`b33460b38ad13b5cd07da75e46444397cf0ea2df`입니다. 이 커밋의 TV 식별 정보 수정이 필요했습니다.
플러그인 디렉터리에 여러 버전을 동시에 두지 않습니다.

## Ubuntu 설치와 재시작

Java 17 이상을 설치하고 `/home/ubuntu/onair-lavalink`를 만듭니다(권한 700).
[공식 Lavalink 릴리스](https://github.com/lavalink-devs/Lavalink/releases/tag/4.2.2)의
`Lavalink.jar`를 이 디렉터리에 받습니다. `plugins/` 안에는 다음 공식 Maven 파일을 둡니다.

```text
https://maven.lavalink.dev/snapshots/dev/lavalink/youtube/youtube-plugin/b33460b38ad13b5cd07da75e46444397cf0ea2df/youtube-plugin-b33460b38ad13b5cd07da75e46444397cf0ea2df.jar
```

`infra/lavalink/application.example.yml`을 `application.yml`로 복사합니다.
같은 디렉터리에 `service.env`를 만들고 권한 600으로 아래 값을 설정합니다.

```dotenv
ONAIR_LAVALINK_PASSWORD=내부서비스전용비밀번호
ONAIR_YOUTUBE_REFRESH_TOKEN=승인후발급된갱신토큰
ONAIR_CIPHER_URL=https://cipher.kikkia.dev/
```

백엔드 `.env`에는 동일한 내부 비밀번호와 `ONAIR_LAVALINK_URL=http://127.0.0.1:2333`를
설정합니다. Google 갱신 토큰은 백엔드에 전달하지 않습니다.
`infra/lavalink/onair-lavalink.service`를 `/etc/systemd/system/`에 복사한 뒤 다음을 실행합니다.
사용자명·경로가 다르면 서비스 파일을 먼저 수정합니다.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now onair-lavalink
sudo systemctl status onair-lavalink --no-pager
```

설정 수정 후 `sudo systemctl restart onair-lavalink`를 실행합니다.
기존 `deploy.sh`는 Lavalink 설치·재시작을 수행하지 않습니다. OAuth 토큰을 Git에 넣지 않습니다.
서버 바깥에 2333 포트를 공개하지 않습니다.

## OAuth 및 장애 처리

최초 승인은 [공식 OAuth 안내](https://github.com/lavalink-devs/youtube-source#using-oauth-tokens)를
따릅니다. 토큰 없이 최초 실행할 때만 OAuth 로그를 INFO로 설정하고, 승인 로그에서 갱신 토큰을
비공개 파일에 보관합니다. 이후 WARN으로 복원하고 재시작합니다.
OAuth 로그·`GET /youtube`·OAuth REST 응답에는 토큰이 포함될 수 있어 공유하거나 공개하지 않습니다.

갱신 토큰이 유효하면 접속 토큰은 자동 갱신됩니다. 권한 취소·계정 제한 등은 재인증이 필요할 수
있습니다. 내부 Lavalink 401/403은 내부 비밀번호 오류이며 Google 재인증과 다릅니다.
백엔드 로그의 `service_auth`, `service_unavailable`, `stream_failed_check_lavalink`로 원인을
좁힌 뒤 관리자만 비공개 Lavalink 로그를 확인합니다. 스트림 500만으로 토큰 만료를 단정하지 않습니다.
재인증 안내는 관리자 로그/운영 가이드 수준이며 별도 운영자 화면·자동 알림은 포함하지 않습니다.

다운로드는 120초, 실제 수신량 100 MiB 이내로 제한합니다. Lavalink의 Content-Length가 매우 큰
센티널 값일 수 있어 실제 바이트 수를 검사합니다. MP4는 일부만 받으면 디코딩이 실패할 수 있어
EOF까지 받은 뒤 변환합니다. 실패하면 큐에 넣지 않으며 기존 송출/폴백이 계속됩니다.
실제 길이 검사에는 FFprobe도 필요합니다(보통 FFmpeg와 함께 설치).
절대 FFmpeg 경로를 쓰면 같은 폴더의 FFprobe를 찾고, `ONAIR_FFPROBE`로 별도 경로를 지정할 수 있습니다.
WEB 메타데이터 조회가 막혀도 OAuth TV 다운로드가 가능할 수 있습니다. 이 경우 제목은 영상 ID,
아티스트는 빈 문자열로 표시하며 받은 파일의 길이·오디오 유무를 검사합니다.

[yt-cipher 공개 서비스](https://github.com/kikkia/yt-cipher)는 가용성을 보장하지 않습니다.
이 서비스로 보내는 것은 서명 처리 정보이며 Google OAuth 토큰은 아닙니다.
장기 운영 시 자체 호스팅을 검토하고, YouTube 변경에 맞춰 버전을 검증 후 갱신합니다.
