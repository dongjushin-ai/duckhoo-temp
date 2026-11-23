# 덕후 매거진 자동 발행 (@duckhoo.mag)

GitHub Actions 가 매일 15:00(주간 릴스) · 18:00(뉴스) · 20:00(뉴스 릴스) · 21:00(큐 1편) KST 에 `bot/ig_publish.py` 를 돌려 인스타에 올려요. PC 는 필요 없어요.

- `posts/queue/` 큐 185편 (`카테고리_NNN_주제.zip`, 2026-11-22 부터 하루 1편)
- `posts/news/` 그때그때 만든 뉴스 (`YYYY-MM-DD_요일_AI뉴스_주제.zip`, 그날 18:00)
- `posts/reels/` 릴스 mp4. 큐 편과 같은 이름이면 그 편 다음 날 15:00, 뉴스(날짜 붙은 편)와 같은 이름이면 그날 20:00 (bot/.env NEWS_REEL_DAYS 로 요일 제한)
- `bot/schedule.csv` 일정 (자동 생성, approved=Y 만 발행), `bot/log.json` 발행 기록
- 멈추기: 저장소 맨 위에 `PAUSE` 라는 파일을 만들면 쉬고, 지우면 다시 돌아요.
- 토큰은 Settings → Secrets → Actions 의 `IG_TOKEN` 에만 있어요.
- 같은 캡션 첫 줄의 게시물이 이미 인스타에 있으면 다시 올리지 않아요.
