# video_reader — 프로젝트 규칙

공통 규칙은 상위 `../CLAUDE.md`. 사용법은 `README.md`, 화면 콘셉트는 `docs/design/concept.md`, 만든 과정은 `docs/history/`.

- 층: 엔진 `engine.py`·`sources.py`(단계: probe·captions·stt 맡기기·split·summarize_section·frames·read_frames·concepts·review_concepts·examples·restructure·overview)
  → 서비스 `service.py`(상태의 유일한 주인, SQLite `data/video_reader.db`) → 제품 API `app.py` + A2A `a2a_tasks.py`(같은 서비스 함수).
  CLI `python -m video_reader read` 는 서버 없이 엔진만 쓴다.
- 글은 사람이 만든 자막(업로더·파일 안·옆 .srt)을 먼저 쓰고, **자동 자막은 쓰지 않는다** (사용자 요구, 2026-11-24). 없으면 stt 에이전트에 A2A 로
  맡긴다 — messageId `video_reader-<작업 id>` 라 재시작해도 같은 stt 작업을 이어 기다린다. stt 쪽 코드를 여기 복사하지 않는다.
- 결과는 **정보 층(빠짐없이)과 정리 층(읽히게) 두 층** (사용자 "정보는 정보대로도 두고 정리는 정리대로", 2026-11-25).
  정보 층 = 구간마다 결론 + 항목(아래에 이유·사례·자료) + 언급된 것, 원문 숫자·이름 누락 검사(`key_tokens`·`coverage`, 빠지면 한 번만 채움).
  정리 층 = 세 줄 결론 · **다시 짠 정리**(story) · 용어와 기준 · 처지별 할 일·가져갈 것 · 구간별 한 줄(+예시), 줄마다 출처 구간. 쓰임새를 바꾸면 정리 층만 다시 쓴다.
- 정리 층은 **영상 순서의 압축이 아니라 다시 짜기** (사용자 "긴 원문 -> 압축 발췌 이 프로세스로하면 안되고 보다 주도적으로 내용을 재구성", 2026-11-26).
  순서: 용어·기준(`concepts`) → 원문에 대 보고 검토(`review_concepts`) → 구간마다 예시(`examples`) → 다시 짜기(`restructure`: 읽는 사람의 질문을 먼저 세우고
  영상 전체에서 답을 모음) → 세 줄 결론·구간별 한 줄(`overview`). 영상 밖 내용은 **표시를 붙일 때만**: 일반 정의 `source=general`·항목 `from_video=false`,
  만든 예시 `source=made`(말한 사람·그 조직의 일처럼 쓰지 않음), 흩어진 내용을 잇는 해석 `kind=interpret`. 그 밖에 정보 층에 없는 말은 쓰지 않는다.
- 겹침 없애기는 코드로: 본문이 푼 목록은 용어 칸에서 다시 나열하지 않음(`expanded_in_story`), 본문에 놓은 예시는 구간별 한 줄에서 뺌(`ex_used`).
- 검토 근거는 용어·항목 이름이 나온 **모든 자리** (`_evidence`) — 처음 나온 구간만 주자 맞는 정의를 "근거 없음"으로 고쳤다. "발췌에 없음"만으로는 고치지 않는다.
  다시 짜기 품질의 목표 모양은 손으로 쓴 정답본 `docs/history/2026-11-26_restructure/reference_fde.md`.
- 구간은 **영상 구성을 따른다**: 올린 사람 챕터 → 말에서 경계 찾기 → 시간. 경계는 "경계 시각 이후 첫 줄" (가장 가까운 줄이면 앞 구간 끝 문장이 넘어온다).
- 시각은 모델이 줄 앞 `[mm:ss]` 를 그대로 옮기고 코드가 변환 (`parse_at`) — 모델에 초 계산을 맡기지 않는다.
- 글이 stt 면 영상 설명·제목의 표기로 이름을 바로잡고(근거 있을 때만), 근거 없는 사람·회사·책·강의 이름은 "받아 적기 확인 필요"(규칙, `needs_check`).
- **정답 있는 시험 영상**: FDE 「FDE, 교육으로 키울 수 있을까?」(dhojLjm7c9w, 12:45) — 업로더 자막·챕터가 있어 stt 정확도(CER)·구성 찾기·정리 결과를
  직접 전사 없이 잰다 (`transcribe: stt` + `sttFresh`). 프롬프트를 바꾸면 이 영상으로 누락·표기·경계를 다시 잰다. 결과 기록 `docs/history/2026-11-25_summary-quality/`, `2026-11-26_restructure/`.
- 확장은 하지 않기로 함 (사용자, 2026-11-25) — 화자 여럿·긴 영상·로컬 LLM 정리·공유 글·영상 묶음 평가는 README "만나면 다룰 케이스" 에서 만났을 때.
- 정리하는 쪽은 **로컬 LLM·Claude Code 둘 다** (사용자 요구). 좁은 `LLM` Protocol(`llm.py`) 하나, 같은 프롬프트. 한쪽만 되는 기능을 만들면
  화면에 "잠김 + 이유"로 드러낸다 (예: 로컬은 지금 그림을 못 받음 → 화면은 붙이고 읽지 않음, 프레임마다 `read` 표시).
- 로컬 LLM 컨텍스트 8,192 토큰 → 구간 예산 `SECTION_TOKENS`(추정). 바꾸면 실제 로컬 LLM 으로 한 번 끝까지 돌려 본다 (넘치면 반으로 나눠 다시).
- 외부 의존은 Protocol 뒤: `Remote`(yt-dlp) · `SttClient`(stt A2A) · `LLM`. 테스트·점검은 이 셋만 가짜, ffmpeg·파일·SQLite 는 진짜.
- 이 PC(WSL)는 닫힌 포트가 거부 대신 연결 시간 초과까지 기다린다 — 켜져 있나 확인은 연결 대기 1초(`PROBE_TIMEOUT`).
- 화면을 고치면 `ui-design` 스킬부터. 고친 뒤 `pytest` 와 `python tools/browser_check.py` 둘 다.
- A2A 결과에는 `metadata.reviewUrl`(= 화면 `#job=<id>`). 화면에서 고친 정리가 A2A 결과·마크다운에도 그대로 나간다.
- 받은 영상 정보·캡처는 `data/` 에만 (git 제외). 문서 캡처는 공개 영상으로만.
