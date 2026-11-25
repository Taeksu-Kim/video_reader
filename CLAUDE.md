# video_reader — 프로젝트 규칙

공통 규칙은 상위 `../CLAUDE.md`. 사용법은 `README.md`, 화면 콘셉트는 `docs/design/concept.md`, 만든 과정은 `docs/history/`.

- 층: 엔진 `engine.py`·`sources.py`(단계: probe·captions·stt 맡기기·split·summarize_section·frames·read_frames·overview)
  → 서비스 `service.py`(상태의 유일한 주인, SQLite `data/video_reader.db`) → 제품 API `app.py` + A2A `a2a_tasks.py`(같은 서비스 함수).
  CLI `python -m video_reader read` 는 서버 없이 엔진만 쓴다.
- 글은 사람이 만든 자막(업로더·파일 안·옆 .srt)을 먼저 쓰고, **자동 자막은 쓰지 않는다** (사용자 요구, 2026-11-24). 없으면 stt 에이전트에 A2A 로
  맡긴다 — messageId `video_reader-<작업 id>` 라 재시작해도 같은 stt 작업을 이어 기다린다. stt 쪽 코드를 여기 복사하지 않는다.
- 결과는 **정보 층(빠짐없이)과 정리 층(읽히게) 두 층** (사용자 "정보는 정보대로도 두고 정리는 정리대로", 2026-11-25).
  정보 층 = 구간마다 결론 + 항목(아래에 이유·사례·자료) + 언급된 것, 원문 숫자·이름 누락 검사(`key_tokens`·`coverage`, 빠지면 한 번만 채움).
  정리 층 = 세 줄 결론·개요·구간별 한 줄·가져갈 것, 줄마다 출처 구간 — 정보 층에 없는 말을 쓰지 않는다. 쓰임새를 바꾸면 정리 층만 다시 쓴다.
- 구간은 **영상 구성을 따른다**: 올린 사람 챕터 → 말에서 경계 찾기 → 시간. 경계는 "경계 시각 이후 첫 줄" (가장 가까운 줄이면 앞 구간 끝 문장이 넘어온다).
- 시각은 모델이 줄 앞 `[mm:ss]` 를 그대로 옮기고 코드가 변환 (`parse_at`) — 모델에 초 계산을 맡기지 않는다.
- 글이 stt 면 영상 설명·제목의 표기로 이름을 바로잡고(근거 있을 때만), 근거 없는 사람·회사·책·강의 이름은 "받아 적기 확인 필요"(규칙, `needs_check`).
- **정답 있는 시험 영상**: FDE 「FDE, 교육으로 키울 수 있을까?」(dhojLjm7c9w, 12:45) — 업로더 자막·챕터가 있어 stt 정확도(CER)·구성 찾기·정리 결과를
  직접 전사 없이 잰다 (`transcribe: stt` + `sttFresh`). 프롬프트를 바꾸면 이 영상으로 누락·표기·경계를 다시 잰다. 결과 기록 `docs/history/2026-11-25_summary-quality/`.
- 확장은 하지 않기로 함 (사용자, 2026-11-25) — 화자 여럿·긴 영상·로컬 LLM 정리·공유 글·영상 묶음 평가는 README "만나면 다룰 케이스" 에서 만났을 때.
- 정리하는 쪽은 **로컬 LLM·Claude Code 둘 다** (사용자 요구). 좁은 `LLM` Protocol(`llm.py`) 하나, 같은 프롬프트. 한쪽만 되는 기능을 만들면
  화면에 "잠김 + 이유"로 드러낸다 (예: 로컬은 지금 그림을 못 받음 → 화면은 붙이고 읽지 않음, 프레임마다 `read` 표시).
- 로컬 LLM 컨텍스트 8,192 토큰 → 구간 예산 `SECTION_TOKENS`(추정). 바꾸면 실제 로컬 LLM 으로 한 번 끝까지 돌려 본다 (넘치면 반으로 나눠 다시).
- 외부 의존은 Protocol 뒤: `Remote`(yt-dlp) · `SttClient`(stt A2A) · `LLM`. 테스트·점검은 이 셋만 가짜, ffmpeg·파일·SQLite 는 진짜.
- 이 PC(WSL)는 닫힌 포트가 거부 대신 연결 시간 초과까지 기다린다 — 켜져 있나 확인은 연결 대기 1초(`PROBE_TIMEOUT`).
- 화면을 고치면 `ui-design` 스킬부터. 고친 뒤 `pytest` 와 `python tools/browser_check.py` 둘 다.
- A2A 결과에는 `metadata.reviewUrl`(= 화면 `#job=<id>`). 화면에서 고친 정리가 A2A 결과·마크다운에도 그대로 나간다.
- 받은 영상 정보·캡처는 `data/` 에만 (git 제외). 문서 캡처는 공개 영상으로만.
