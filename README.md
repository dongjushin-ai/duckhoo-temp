# 덕후 매거진 자동 발행 (@duckhoo.mag)

GitHub Actions 가 매일 18:00 · 20:00 KST 에 `bot/ig_publish.py` 를 돌려 인스타에 올려요. PC 는 필요 없어요.

**v6 (2026-11-23 컨설팅 리뉴얼): 주 5회, 20:00 KST**
- 월·목 20:00 오리 편집장 릴스 (`posts/reels/시트콤_NNN_주제.mp4` + 같은 이름 `.txt` 캡션)
- 화·토 20:00 8장 캐러셀 (`posts/queue/비교|레시피|용어_NNN_주제.zip`)
- 금 20:00 뉴스 릴스 1편 (`posts/news` + 같은 이름 mp4, `NEWS_REEL_DAYS=금`)
- 번호 NNN 은 `QUEUE_SLOTS=월20:00,화20:00,목20:00,토20:00` 의 몇 번째 칸인지 (QUEUE_START 2026-11-24 부터)
- 예전 185편 큐와 27편 릴스는 `archive/` 에 보관 (발행 안 됨)

- `posts/news/` 그때그때 만든 뉴스 (`YYYY-MM-DD_요일_AI뉴스_주제.zip`, 그날 18:00)
- `bot/schedule.csv` 일정 (자동 생성, approved=Y 만 발행), `bot/log.json` 발행 기록
- 멈추기: 저장소 맨 위에 `PAUSE` 라는 파일을 만들면 쉬고, 지우면 다시 돌아요.
- 토큰은 Settings → Secrets → Actions 의 `IG_TOKEN` 에만 있어요.
- 같은 캡션 첫 줄의 게시물이 이미 인스타에 있으면 다시 올리지 않아요.
