#!/usr/bin/env python3
"""덕후 매거진 인스타그램 자동 발행 (Instagram API with Instagram Login).

파이썬 3.9+ 표준 라이브러리만 씀 (pip 설치 필요 없음).

명령:
  python ig_publish.py check                  토큰·계정·발행 한도·호스팅 확인 (아무것도 안 올림)
  python ig_publish.py plan                   게시물 폴더를 읽어 schedule.csv 만들기/갱신
  python ig_publish.py publish <폴더이름>      드라이런: 검사만 하고 무엇을 올릴지 보여줌
  python ig_publish.py publish <폴더이름> --go 지금 바로 실제 게시
  python ig_publish.py run                    드라이런: 지금 올릴 차례인 게시물 보여줌
  python ig_publish.py run --go               schedule.csv 에서 approved=Y 이고 시간이 된 것만 게시
  python ig_publish.py reel <폴더이름>         드라이런: 릴스 영상 검사만
  python ig_publish.py reel <폴더이름> --go    그 편의 릴스를 지금 바로 게시
  python ig_publish.py refresh                장기 토큰 연장 (run 이 자동으로도 함)

게시물 이름은 두 가지 다 읽어요.
  · 순서 큐: 비교_001_AI3대장(.zip)  → QUEUE_SLOTS(예: 월20:00,화20:00,목20:00,토20:00)의 번호째 칸.
    QUEUE_SLOTS 가 없으면 날짜 = QUEUE_START + (번호-1)일, POST_TIME.
    릴스만 있는 큐 편: REELS_DIR 의 시트콤_002_주제.mp4 (+ 같은 이름 .txt 캡션) → 그 번호 칸에 릴스로.
  · 예전 방식: 2026-11-24_화_커버스토리_01_2026AI지도(.zip)
뉴스 릴스: 날짜 붙은 편(posts/news)과 같은 이름의 mp4 가 REELS_DIR 에 있으면 그날 NEWS_REEL_TIME(기본 20:00)에 릴스. NEWS_REEL_DAYS(예: 금)면 그 요일만.
          캐러셀 없이 릴스만: posts/reels/YYYY-MM-DD_요일_AI뉴스_주제.mp4 + 같은 이름 .txt(캡션) 만 넣으면 됨.
릴스: 고른 편만 (주 1편 정도). REELS_DIR 에 <게시물이름>.mp4 를 넣은 편만 릴스가 돼요.
      plan 이 그 편 캐러셀 다음 날 REEL_TIME(기본 12:30)에 릴스 줄을 만들어요 (캐러셀도 그대로 올라감).
      REEL_DAYS(예: 수) 를 적으면 다음 날 이후 첫 그 요일로 잡아요. 영상은 Meta 에 직접 올려서 GitHub 불필요.

설정은 같은 폴더의 .env (예시는 .env.example). 토큰은 채팅에 붙여넣지 말고 .env 에만.
"""
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
SCHEDULE_PATH = HERE / "schedule.csv"
LOG_PATH = HERE / "log.json"
CACHE_DIR = HERE / "_cache"
KST = timezone(timedelta(hours=9))  # 윈도우엔 tzdata 가 없을 수 있어 고정 오프셋 사용

NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_[^_]+_(.+?)(\.zip)?$")
QUEUE_RE = re.compile(r"^([0-9A-Za-z가-힣]+)_(\d{3})_([^/]+?)(\.zip)?$")  # 카테고리_NNN_주제
WEEKDAYS = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
CARD_RE = re.compile(r"^\d{2}\.jpe?g$", re.I)


class Fail(Exception):
    pass


# ---------------------------------------------------------------- 설정

def load_env():
    if not ENV_PATH.exists():
        raise Fail(f".env 가 없어요: {ENV_PATH}\n  .env.example 을 복사해서 .env 로 이름 바꾸고 값을 채워 주세요.")
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():  # .env 에 없는 값만 환경변수에서 채움
        if k not in env and k.startswith(("IG_", "GITHUB_", "POSTS_DIR", "POST_TIME", "GRACE_HOURS", "API_",
                                          "QUEUE_", "REEL", "NEWS_", "AUTO_")):
            env[k] = v
    return env


def save_env_values(updates):
    lines = ENV_PATH.read_text(encoding="utf-8-sig").splitlines()
    done = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if k in updates and "=" in line and not line.strip().startswith("#"):
            lines[i] = f"{k}={updates[k]}"
            done.add(k)
    for k, v in updates.items():
        if k not in done:
            lines.append(f"{k}={v}")
    tmp = ENV_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(ENV_PATH)


def need(env, *keys):
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise Fail(".env 에 값이 비어 있어요: " + ", ".join(missing))


# ---------------------------------------------------------------- HTTP

def http(method, url, params=None, body=None, headers=None, token=None):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        if token:
            raw = raw.replace(token, "***")
        safe_url = url.split("?")[0]
        raise Fail(f"{method} {safe_url} 실패 ({e.code}): {raw[:600]}")
    except urllib.error.URLError as e:
        raise Fail(f"{method} {url.split('?')[0]} 연결 실패: {e.reason}")
    except OSError as e:  # 연결 끊김 등
        raise Fail(f"{method} {url.split('?')[0]} 연결 실패: {e}")
    if "json" in ctype or raw[:1] in (b"{", b"["):
        return json.loads(raw.decode("utf-8"))
    return raw


class IG:
    def __init__(self, env):
        need(env, "IG_TOKEN", "IG_USER_ID")
        self.token = env["IG_TOKEN"]
        self.uid = env["IG_USER_ID"]
        self.base = env.get("API_BASE", "https://graph.instagram.com").rstrip("/")
        self.ver = env.get("API_VERSION", "v25.0")
        # 새 앱(통합 로그인·시스템 사용자 토큰)은 graph.facebook.com, 예전 인스타 로그인 앱은 graph.instagram.com
        self.fb = "facebook.com" in self.base

    def _u(self, path):
        return f"{self.base}/{self.ver}/{path.lstrip('/')}"

    def get(self, path, **params):
        params["access_token"] = self.token
        return http("GET", self._u(path), params=params, token=self.token)

    def post(self, path, **params):
        params["access_token"] = self.token
        body = urllib.parse.urlencode(params).encode()
        return http("POST", self._u(path), body=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"}, token=self.token)

    def refresh(self):
        return http("GET", f"{self.base}/refresh_access_token",
                    params={"grant_type": "ig_refresh_token", "access_token": self.token}, token=self.token)


# ---------------------------------------------------------------- 이미지 임시 보관 (GitHub 공개 저장소)

class Host:
    """게시 순간에만 이미지를 공개 GitHub 저장소에 올리고(raw 주소로 Meta 가 가져감), 게시가 끝나면 지운다."""

    def __init__(self, env):
        need(env, "GITHUB_TOKEN", "GITHUB_REPO")
        self.key = env["GITHUB_TOKEN"]
        self.repo = env["GITHUB_REPO"].strip().strip("/")
        self.branch = env.get("GITHUB_BRANCH", "main")
        self.api = env.get("GITHUB_API", "https://api.github.com").rstrip("/")
        self.raw = env.get("GITHUB_RAW", "https://raw.githubusercontent.com").rstrip("/")
        self.sha = {}

    def _h(self):
        return {"Authorization": f"Bearer {self.key}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "duckhoo-publisher",
                "Content-Type": "application/json"}

    def upload(self, key, data):
        import base64
        body = {"message": f"tmp {key}", "content": base64.b64encode(data).decode(), "branch": self.branch}
        try:
            cur = http("GET", f"{self.api}/repos/{self.repo}/contents/{key}", params={"ref": self.branch},
                       headers=self._h(), token=self.key)
            body["sha"] = cur["sha"]
        except Fail:
            pass
        res = http("PUT", f"{self.api}/repos/{self.repo}/contents/{key}", body=json.dumps(body).encode(),
                   headers=self._h(), token=self.key)
        self.sha[key] = res["content"]["sha"]
        return f"{self.raw}/{self.repo}/{self.branch}/{key}"

    def wait_public(self, url, tries=12):
        for _ in range(tries):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=20) as r:
                    if r.status == 200 and "image/jpeg" in r.headers.get("Content-Type", ""):
                        return True
            except Exception:
                pass
            time.sleep(5)
        return False

    def delete(self, keys):
        for key in keys:
            body = {"message": f"cleanup {key}", "sha": self.sha.get(key), "branch": self.branch}
            http("DELETE", f"{self.api}/repos/{self.repo}/contents/{key}", body=json.dumps(body).encode(),
                 headers=self._h(), token=self.key)


# ---------------------------------------------------------------- 게시물 읽기·검사

def jpeg_size(data):
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def find_posts(env):
    need(env, "POSTS_DIR")
    root = Path(env["POSTS_DIR"]).expanduser()
    if not root.is_dir():
        raise Fail(f"POSTS_DIR 폴더가 없어요: {root}")
    start = None
    if env.get("QUEUE_START"):
        try:
            start = datetime.strptime(env["QUEUE_START"].strip(), "%Y-%m-%d").date()
        except ValueError:
            raise Fail(f"QUEUE_START 형식은 2026-11-21 처럼: {env['QUEUE_START']}")
    posts = {}
    slots = queue_slots(env, start)
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() == ".mp4" and "_cache" not in p.parts:  # 릴스만 있는 큐 편 (v6 시트콤 등)
            q = QUEUE_RE.match(p.stem)
            if q:
                num = int(q.group(2))
                d_, t_ = slots(num) if start else ("", "")
                posts[p.stem] = {"name": p.stem, "date": d_, "time": t_, "num": num, "path": p, "kind": "reel"}
            continue
        if "_cache" in p.parts or not (p.is_dir() or (p.is_file() and p.suffix.lower() == ".zip")):
            continue
        name = p.name[:-4] if p.name.lower().endswith(".zip") else p.name
        m, q = NAME_RE.match(p.name), QUEUE_RE.match(p.name)
        if m:
            date, num, qtime = m.group(1), None, ""
        elif q:
            num = int(q.group(2))
            date, qtime = slots(num) if start else ("", "")
        else:
            continue
        if p.is_dir() and q and p.parent.name == name:  # zip 을 풀면 생기는 같은 이름 하위 폴더는 건너뜀
            continue
        if p.is_dir() or name not in posts:  # 폴더가 있으면 zip 보다 우선
            posts[name] = {"name": name, "date": date, "time": qtime, "num": num, "path": p}
    return posts


def queue_slots(env, start):
    """번호 → (날짜, 시각). QUEUE_SLOTS(예: 월20:00,화20:00,목20:00,토20:00)가 있으면 QUEUE_START 부터 그 칸을 차례로,
    없으면 예전처럼 하루 1편(POST_TIME)."""
    spec = [x.strip() for x in env.get("QUEUE_SLOTS", "").split(",") if x.strip()]
    t0 = env.get("POST_TIME", "21:00")
    if not spec or not start:
        return lambda n: ((start + timedelta(days=n - 1)).isoformat() if start else "", t0)
    want = {}
    for x in spec:
        if x[0] not in WEEKDAYS:
            raise Fail(f"QUEUE_SLOTS 형식은 월20:00,목20:00 처럼: {x}")
        want[WEEKDAYS[x[0]]] = x[1:].strip() or t0
    def at(n):
        d, k = start, 0
        while True:
            if d.weekday() in want:
                k += 1
                if k == n:
                    return d.isoformat(), want[d.weekday()]
            d += timedelta(days=1)
    return at


def reels_dir(env):
    d = env.get("REELS_DIR", "").strip()
    return Path(d).expanduser() if d else Path(env.get("POSTS_DIR", ".")).expanduser() / "reels"


def find_reel(env, name):
    f = reels_dir(env) / f"{name}.mp4"
    return f if f.is_file() else None


def reel_only(p):
    return p and p.get("kind") == "reel"


def reel_caption(env, name, post):
    f = reels_dir(env) / f"{name}.txt"  # 릴스 전용 캡션이 있으면 그걸, 없으면 캐러셀 캡션
    if f.is_file():
        return f.read_text(encoding="utf-8-sig").strip()
    return open_post(post)[1] if post else ""


def check_reel(path, caption):
    problems = []
    data = path.read_bytes()[:64]
    if data[4:8] != b"ftyp":
        problems.append(f"{path.name}: MP4 가 아님")
    size = path.stat().st_size
    if size > 300 * 1024 * 1024:
        problems.append(f"{path.name}: {size // 2**20}MB (릴스 최대 300MB)")
    if not caption:
        problems.append("캡션 없음")
    if len(caption) > 2200:
        problems.append(f"캡션 {len(caption)}자 (최대 2200)")
    return problems


def open_post(post):
    p = post["path"]
    if p.is_file():  # zip 이면 _cache 에 풀어서 읽기
        dest = CACHE_DIR / post["name"]
        if dest.exists() and dest.stat().st_mtime < p.stat().st_mtime:  # zip 이 새로 바뀌었으면 다시 풀기
            shutil.rmtree(dest)
        if not dest.exists():
            with zipfile.ZipFile(p) as z:
                for info in z.infolist():
                    fname = Path(info.filename).name
                    if info.is_dir() or not (CARD_RE.match(fname) or fname == "caption.txt"):
                        continue
                    dest.mkdir(parents=True, exist_ok=True)
                    (dest / fname).write_bytes(z.read(info))
        p = dest
    else:
        sub = [d for d in p.iterdir() if d.is_dir()]
        if not any(CARD_RE.match(f.name) for f in p.iterdir()) and len(sub) == 1:
            p = sub[0]
    cards = sorted(f for f in p.iterdir() if CARD_RE.match(f.name))
    cap = p / "caption.txt"
    caption = cap.read_text(encoding="utf-8-sig").strip() if cap.exists() else ""
    return cards, caption


def check_post(cards, caption):
    problems = []
    if not 2 <= len(cards) <= 10:
        problems.append(f"장수 {len(cards)}장: API 캐러셀은 2~10장만 됨")
    first_ratio = None
    for f in cards:
        data = f.read_bytes()
        size = jpeg_size(data)
        if not size:
            problems.append(f"{f.name}: JPEG 이 아님")
            continue
        w, h = size
        r = w / h
        first_ratio = first_ratio or r
        if not (0.8 - 0.005 <= r <= 1.91 + 0.005):
            problems.append(f"{f.name}: 비율 {w}x{h} (4:5~1.91:1 범위 밖)")
        if abs(r - first_ratio) > 0.01:
            problems.append(f"{f.name}: 첫 장과 비율이 달라 잘릴 수 있음")
        if len(data) > 8 * 1024 * 1024:
            problems.append(f"{f.name}: 8MB 초과")
    if not caption:
        problems.append("caption.txt 없음/비어 있음")
    if len(caption) > 2200:
        problems.append(f"캡션 {len(caption)}자 (최대 2200)")
    if len(re.findall(r"#\w+", caption)) > 30:
        problems.append("해시태그 30개 초과")
    if len(re.findall(r"@[\w.]+", caption)) > 20:
        problems.append("@멘션 20개 초과")
    return problems


# ---------------------------------------------------------------- 기록·일정

def load_log():
    return json.loads(LOG_PATH.read_text(encoding="utf-8")) if LOG_PATH.exists() else {}


def save_log(log):
    tmp = LOG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LOG_PATH)


def load_schedule():
    if not SCHEDULE_PATH.exists():
        return []
    with open(SCHEDULE_PATH, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_schedule(rows):
    with open(SCHEDULE_PATH, "w", encoding="utf-8-sig", newline="") as f:  # 엑셀에서 한글 안 깨지게 BOM
        w = csv.DictWriter(f, fieldnames=["date", "time", "type", "folder", "approved", "status"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})


def now_kst():
    return datetime.now(KST)


# ---------------------------------------------------------------- 토큰

def maybe_refresh(env, force=False):
    if "facebook.com" in env.get("API_BASE", ""):
        if force:
            print("시스템 사용자 토큰은 만료가 없어서 연장할 필요가 없어요.")
        return
    issued = env.get("IG_TOKEN_ISSUED", "")
    today = now_kst().date()
    if not issued:
        save_env_values({"IG_TOKEN_ISSUED": today.isoformat()})
        print("IG_TOKEN_ISSUED 를 오늘로 적어 뒀어요 (60일 뒤 만료 기준).")
        return
    age = (today - datetime.strptime(issued, "%Y-%m-%d").date()).days
    if age >= 55:
        print(f"⚠ 토큰 발급 {age}일째. 60일이 지나면 연장 불가라 대시보드에서 새로 발급해야 해요.")
    if force or age >= 30:
        if age < 1:
            print("토큰이 하루가 안 돼서 아직 연장 못 해요 (Meta 규칙).")
            return
        res = IG(env).refresh()
        save_env_values({"IG_TOKEN": res["access_token"], "IG_TOKEN_ISSUED": today.isoformat()})
        env["IG_TOKEN"] = res["access_token"]
        env["IG_TOKEN_ISSUED"] = today.isoformat()
        days = int(res.get("expires_in", 0)) // 86400
        print(f"토큰 연장 완료 (앞으로 약 {days}일).")


# ---------------------------------------------------------------- 명령

def find_ig_ids(env):
    """IG_USER_ID 를 모를 때: 토큰으로 볼 수 있는 인스타 계정 ID 찾기 (새 앱 방식)."""
    base = env.get("API_BASE", "https://graph.instagram.com").rstrip("/")
    ver = env.get("API_VERSION", "v25.0")
    tok = env["IG_TOKEN"]
    found = {}
    tries = [("me/businesses", "owned_instagram_accounts{id,username},instagram_accounts{id,username}"),
             ("me/accounts", "instagram_business_account{id,username}")]
    for path, fields in tries:
        try:
            res = http("GET", f"{base}/{ver}/{path}", params={"fields": fields, "access_token": tok}, token=tok)
        except Fail as e:
            print(f"  ({path} 조회 실패: {str(e)[:160]})")
            continue
        for item in res.get("data", []):
            for k in ("owned_instagram_accounts", "instagram_accounts"):
                for a in (item.get(k) or {}).get("data", []):
                    found[a["id"]] = a.get("username")
            a = item.get("instagram_business_account")
            if a:
                found[a["id"]] = a.get("username")
    return found


def cmd_check(env, args):
    if env.get("IG_TOKEN") and not env.get("IG_USER_ID", "").isdigit():
        print("IG_USER_ID 가 비어 있어서 토큰으로 찾아볼게요…")
        found = find_ig_ids(env)
        for i, u in found.items():
            print(f"  @{u}  →  IG_USER_ID={i}")
        if not found:
            print("  못 찾았어요. 비즈니스 설정 > Instagram 계정에서 숫자 ID 를 확인해 주세요.")
            return
        if len(found) == 1:
            env["IG_USER_ID"] = next(iter(found))
            save_env_values({"IG_USER_ID": env["IG_USER_ID"]})
            print("  .env 에 적어 뒀어요.")
        else:
            print("  위에서 duckhoo.mag 줄의 숫자를 .env 의 IG_USER_ID= 에 넣고 다시 check 하세요.")
            return
    ig = IG(env)
    if ig.fb:
        me = ig.get(ig.uid, fields="id,username,name")
        print(f"계정: @{me.get('username')}  ({me.get('name')})  IG 비즈니스 ID: {me.get('id')}")
    else:
        me = ig.get("me", fields="user_id,username,account_type")
        print(f"계정: @{me.get('username')}  유형: {me.get('account_type')}  user_id: {me.get('user_id')}")
        if str(me.get("user_id")) != str(ig.uid):
            print(f"⚠ .env 의 IG_USER_ID({ig.uid}) 와 다름. {me.get('user_id')} 로 바꿔 주세요.")
    lim = ig.get(f"{ig.uid}/content_publishing_limit", fields="config,quota_usage")
    d = (lim.get("data") or [{}])[0]
    print(f"24시간 발행 사용량: {d.get('quota_usage')} / {d.get('config', {}).get('quota_total')}")
    maybe_refresh(env)
    if env.get("GITHUB_REPO"):
        host = Host(env)
        cards = []
        try:
            posts = find_posts(env)
            if posts:
                cards, _ = open_post(next(iter(posts.values())))
        except Fail:
            pass
        data = cards[0].read_bytes() if cards else b"\xff\xd8\xff\xd9"
        key = f"_check/{int(time.time())}.jpg"
        url = host.upload(key, data)
        ok = host.wait_public(url)
        host.delete([key])
        print("이미지 보관: " + ("OK (공개 주소로 JPEG 가 열림, 테스트 파일 삭제함)" if ok else f"⚠ 공개 주소가 안 열려요. 저장소가 Public 인지 확인: {url}"))
    print("점검 끝. 아무것도 게시하지 않았어요.")


def row_type(r):
    return (r.get("type") or "carousel").strip().lower()


def log_key(kind, name):
    return name if kind == "carousel" else f"reel:{name}"


def cmd_plan(env, args):
    posts = find_posts(env)
    old = {(row_type(r), r["folder"]): r for r in load_schedule()}
    log = load_log()
    t = env.get("POST_TIME", "21:00")
    rt = env.get("REEL_TIME", "12:30")
    rdays = {WEEKDAYS[c] for c in env.get("REEL_DAYS", "") if c in WEEKDAYS}
    rows, seen, undated = [], set(), 0
    for name, p in sorted(posts.items()):
        if reel_only(p):  # 릴스만 있는 큐 편: 자기 칸(QUEUE_SLOTS)에 릴스 한 줄
            rkey = ("reel", name)
            rr = old.get(rkey, {"date": p["date"], "time": p["time"], "type": "reel", "folder": name,
                                "approved": env.get("AUTO_APPROVE", "")})
            if not log.get(log_key("reel", name)):
                rr["date"], rr["time"] = p["date"], p["time"]
            rr["type"] = "reel"
            rr["status"] = log.get(log_key("reel", name), {}).get("state", "")
            undated += not rr.get("date")
            rows.append(rr)
            seen.add(rkey)
            continue
        key = ("carousel", name)
        tt = (p.get("time") or t) if p.get("num") else env.get("NEWS_TIME", "18:00")  # 날짜 붙은 편(뉴스 등)은 18:00
        r = old.get(key, {"date": p["date"], "time": tt, "type": "carousel", "folder": name,
                          "approved": env.get("AUTO_APPROVE", "")})
        if not r.get("date") and p["date"]:
            r["date"] = p["date"]
        if p.get("num") and not log.get(name):  # 아직 안 올린 큐 편은 지금 칸 설정을 따름
            r["date"], r["time"] = p["date"] or r.get("date", ""), tt
        undated += not r.get("date")
        r["type"] = "carousel"
        r["status"] = log.get(name, {}).get("state", "")
        rows.append(r)
        seen.add(key)
        if find_reel(env, name) and r.get("date"):
            if p.get("num"):  # 큐 편: 릴스로 고른 편(mp4 있음)만, 캐러셀 다음 날(또는 그 뒤 첫 REEL_DAYS 요일) REEL_TIME
                nxt = datetime.strptime(r["date"], "%Y-%m-%d").date() + timedelta(days=1)
                while rdays and nxt.weekday() not in rdays:
                    nxt += timedelta(days=1)
                rtime = rt
            else:  # 날짜 붙은 편(뉴스): 같은 날 NEWS_REEL_TIME. NEWS_REEL_DAYS(예: 금)를 적으면 그 요일 뉴스만 릴스
                nxt = datetime.strptime(r["date"], "%Y-%m-%d").date()
                rtime = env.get("NEWS_REEL_TIME", "20:00")
                ndays = {WEEKDAYS[c] for c in env.get("NEWS_REEL_DAYS", "") if c in WEEKDAYS}
                if ndays and nxt.weekday() not in ndays:
                    continue
            rkey = ("reel", name)
            if True:
                rr = old.get(rkey, {"date": nxt.isoformat(), "time": rtime, "type": "reel", "folder": name,
                                    "approved": env.get("AUTO_APPROVE", "")})
                if not log.get(log_key("reel", name)):  # 아직 안 올린 줄은 지금 설정 시각으로 맞춤
                    rr["time"] = rtime
                rr["type"] = "reel"
                rr["status"] = log.get(log_key("reel", name), {}).get("state", "")
                rows.append(rr)
                seen.add(rkey)
    # 릴스만 있는 뉴스(캐러셀 zip 없이 posts/reels/YYYY-MM-DD_요일_AI뉴스_주제.mp4 + 같은 이름 .txt 캡션)
    ndays = {WEEKDAYS[c] for c in env.get("NEWS_REEL_DAYS", "") if c in WEEKDAYS}
    rd = reels_dir(env)
    for f in sorted(rd.glob("*.mp4")) if rd.is_dir() else []:
        name, m = f.stem, NAME_RE.match(f.stem)
        if not m or name in posts:
            continue
        d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        if ndays and d.weekday() not in ndays:
            continue
        rkey = ("reel", name)
        rr = old.get(rkey, {"date": d.isoformat(), "time": env.get("NEWS_REEL_TIME", "20:00"), "type": "reel",
                            "folder": name, "approved": env.get("AUTO_APPROVE", "")})
        if not log.get(log_key("reel", name)):
            rr["time"] = env.get("NEWS_REEL_TIME", "20:00")
        rr["type"] = "reel"
        rr["status"] = log.get(log_key("reel", name), {}).get("state", "")
        rows.append(rr)
        seen.add(rkey)
    for key, r in old.items():
        if key not in seen:
            if key[0] == "reel" and log_key("reel", key[1]) not in log:
                continue  # 릴스 후보에서 빠진 편(mp4 없앰, NEWS_REEL_DAYS 밖)은 안 올렸으면 줄도 지움
            if key[0] == "carousel" and key[1] not in log:
                continue  # 큐에서 빠진 편(보관함으로 옮김)은 안 올렸으면 줄도 지움
            r["status"] = "영상 없음" if key[0] == "reel" else "폴더 없음"
            rows.append(r)
    rows.sort(key=lambda r: (r.get("date") or "9999", r.get("time", ""), r["folder"]))
    save_schedule(rows)
    n_reel = sum(row_type(r) == "reel" for r in rows)
    weeks = {}
    for r in rows:
        if row_type(r) == "reel" and r.get("date") and not NAME_RE.match(r["folder"]) and not reel_only(posts.get(r["folder"])):  # 주 1편 목표는 큐 릴스만 (뉴스 릴스는 매일)
            wk = datetime.strptime(r["date"], "%Y-%m-%d").date().isocalendar()[:2]
            weeks.setdefault(wk, []).append(r["folder"])
    for wk, names in sorted(weeks.items()):
        if len(names) > 1:
            print(f"⚠ {wk[0]}년 {wk[1]}주차에 릴스 {len(names)}편 (목표 주 1편): " + ", ".join(names))
    print(f"schedule.csv: {len(rows)}줄 (릴스 {n_reel}). approved 칸에 Y 를 적은 줄만 run --go 로 올라가요.")
    if undated:
        print(f"⚠ 날짜가 빈 편 {undated}개: .env 에 QUEUE_START=첫 게시 날짜(예: 2026-11-21) 를 적고 plan 을 다시 돌리세요.")


def already_live(env, caption, reel=False):
    """인스타에 같은 캡션 첫 줄의 게시물이 이미 있으면 그 permalink (다른 곳에서 먼저 올린 경우 중복 방지)."""
    first = (caption.strip().splitlines() or [""])[0].strip()
    if not first:
        return None
    ig = IG(env)
    try:
        res = ig.get(f"{ig.uid}/media", fields="caption,media_type,media_product_type,permalink", limit="40")
    except Fail as e:
        print(f"  (기존 게시물 확인 실패, 계속 진행: {e})")
        return None
    for m in res.get("data", []):
        is_reel = m.get("media_product_type") == "REELS"
        if is_reel != reel:
            continue
        if (m.get("caption") or "").strip().splitlines()[:1] == [first]:
            return m.get("permalink") or m.get("id")
    return None


def publish_one(env, post, go):
    name = post["name"]
    log = load_log()
    if name in log:
        st = log[name].get("state")
        if st == "published":
            print(f"[건너뜀] {name}: 이미 게시됨 {log[name].get('permalink', '')}")
            return False
        if st == "publishing":
            print(f"[멈춤] {name}: 지난번 게시 도중 끊겼어요. 인스타에서 올라갔는지 직접 보고, "
                  f"안 올라갔으면 log.json 에서 이 항목을 지운 뒤 다시 실행하세요.")
            return False
    cards, caption = open_post(post)
    problems = check_post(cards, caption)
    print(f"\n▶ {name}  ({len(cards)}장, 캡션 {len(caption)}자)")
    print("  캡션 첫 줄: " + (caption.splitlines()[0] if caption else "(없음)"))
    if problems:
        for pr in problems:
            print("  ✗ " + pr)
        print("  → 문제가 있어 게시하지 않아요.")
        return False
    if not go:
        print("  ✓ 검사 통과. 드라이런이라 아무것도 안 올렸어요 (실제로 올리려면 --go).")
        return False
    live = already_live(env, caption)
    if live:
        log[name] = {"state": "published", "permalink": live, "note": "이미 인스타에 있음",
                     "at": now_kst().isoformat(timespec="seconds")}
        save_log(log)
        print(f"  [건너뜀] 이미 인스타에 같은 편이 있어요: {live}")
        return False

    ig, host = IG(env), Host(env)
    prefix = post["date"] + "_" + hashlib.sha1(name.encode()).hexdigest()[:10]
    keys, children = [], []
    try:
        for f in cards:
            key = f"{prefix}/{f.name.lower()}"
            url = host.upload(key, f.read_bytes())
            keys.append(key)
            if not host.wait_public(url):
                raise Fail(f"{f.name}: 공개 주소가 1분 안에 안 열려요 ({url})")
            c = ig.post(f"{ig.uid}/media", image_url=url, is_carousel_item="true")
            children.append(c["id"])
            print(f"  · {f.name} 컨테이너 {c['id']}")
        car = ig.post(f"{ig.uid}/media", media_type="CAROUSEL", children=",".join(children), caption=caption)
        cid = car["id"]
        wait_container(ig, cid, what="캐러셀 컨테이너")  # 최대 약 5분
        log[name] = {"state": "publishing", "container": cid, "at": now_kst().isoformat(timespec="seconds")}
        save_log(log)
        try:
            res = ig.post(f"{ig.uid}/media_publish", creation_id=cid)
        except Fail as e:  # 서버가 오류로 답했으면 게시 안 된 것 → 다음에 재시도 가능
            log[name] = {"state": "error", "error": str(e)[:300], "at": now_kst().isoformat(timespec="seconds")}
            save_log(log)
            raise
        mid = res["id"]
        log[name] = {"state": "published", "media_id": mid, "at": now_kst().isoformat(timespec="seconds")}
        save_log(log)
        try:
            log[name]["permalink"] = ig.get(mid, fields="permalink").get("permalink", "")
            save_log(log)
        except Fail:
            pass
        print(f"  ✓ 게시 완료: {log[name].get('permalink') or mid}")
        return True
    finally:
        try:
            host.delete(keys)
        except Fail as e:
            print(f"  (임시 이미지 삭제 실패, 나중에 버킷에서 지워도 됨: {e})")


def wait_container(ig, cid, tries=30, every=10, what="컨테이너"):
    for _ in range(tries):
        st = ig.get(cid, fields="status_code").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise Fail(f"{what} 상태 {st}")
        time.sleep(every)
    raise Fail(f"{what} 준비가 {tries * every // 60}분 안에 안 끝났어요 (나중에 다시 시도)")


def publish_reel(env, post, go, name=None):
    name = name or post["name"]
    key = log_key("reel", name)
    log = load_log()
    st = log.get(key, {}).get("state")
    if st == "published":
        print(f"[건너뜀] 릴스 {name}: 이미 게시됨 {log[key].get('permalink', '')}")
        return False
    if st == "publishing":
        print(f"[멈춤] 릴스 {name}: 지난번 게시 도중 끊겼어요. 인스타에서 확인하고, 안 올라갔으면 "
              f"log.json 에서 \"{key}\" 항목을 지운 뒤 다시 실행하세요.")
        return False
    path = find_reel(env, name)
    if not path:
        print(f"[멈춤] 릴스 {name}: {reels_dir(env) / (name + '.mp4')} 가 없어요")
        return False
    caption = reel_caption(env, name, post)
    problems = check_reel(path, caption)
    size = path.stat().st_size
    print(f"\n▶ 릴스 {name}  ({size / 2**20:.1f}MB, 캡션 {len(caption)}자)")
    print("  캡션 첫 줄: " + (caption.splitlines()[0] if caption else "(없음)"))
    if problems:
        for pr in problems:
            print("  ✗ " + pr)
        print("  → 문제가 있어 게시하지 않아요.")
        return False
    if not go:
        print("  ✓ 검사 통과. 드라이런이라 아무것도 안 올렸어요 (실제로 올리려면 --go).")
        return False
    live = already_live(env, caption, reel=True)
    if live:
        log[key] = {"state": "published", "permalink": live, "note": "이미 인스타에 있음",
                    "at": now_kst().isoformat(timespec="seconds")}
        save_log(log)
        print(f"  [건너뜀] 이미 인스타에 같은 릴스가 있어요: {live}")
        return False
    ig = IG(env)
    params = {"media_type": "REELS", "upload_type": "resumable", "caption": caption,
              "share_to_feed": env.get("REEL_SHARE_TO_FEED", "true")}
    if env.get("REEL_THUMB_MS"):
        params["thumb_offset"] = env["REEL_THUMB_MS"]
    c = ig.post(f"{ig.uid}/media", **params)
    cid = c["id"]
    up = c.get("uri") or f"{env.get('RUPLOAD_BASE', 'https://rupload.facebook.com').rstrip('/')}/ig-api-upload/{ig.ver}/{cid}"
    res = http("POST", up, body=path.read_bytes(), token=ig.token,
               headers={"Authorization": f"OAuth {ig.token}", "offset": "0", "file_size": str(size),
                        "Content-Type": "application/octet-stream"})
    if isinstance(res, dict) and res.get("success") is False:
        raise Fail(f"영상 업로드 실패: {str(res)[:300]}")
    print(f"  · 영상 업로드 완료, 컨테이너 {cid} 처리 기다리는 중")
    wait_container(ig, cid, tries=60, every=10, what="릴스 컨테이너")  # 최대 약 10분
    log[key] = {"state": "publishing", "container": cid, "at": now_kst().isoformat(timespec="seconds")}
    save_log(log)
    try:
        res = ig.post(f"{ig.uid}/media_publish", creation_id=cid)
    except Fail as e:
        log[key] = {"state": "error", "error": str(e)[:300], "at": now_kst().isoformat(timespec="seconds")}
        save_log(log)
        raise
    mid = res["id"]
    log[key] = {"state": "published", "media_id": mid, "at": now_kst().isoformat(timespec="seconds")}
    save_log(log)
    try:
        log[key]["permalink"] = ig.get(mid, fields="permalink").get("permalink", "")
        save_log(log)
    except Fail:
        pass
    print(f"  ✓ 릴스 게시 완료: {log[key].get('permalink') or mid}")
    return True


def pick_post(posts, q):
    hits = [p for n, p in posts.items() if n == q] or [p for n, p in posts.items() if q in n]
    if len(hits) != 1:
        raise Fail(f"'{q}' 에 맞는 게시물이 {len(hits)}개예요: " + ", ".join(p["name"] for p in hits[:8]))
    return hits[0]


def cmd_reel(env, args):
    if not args or args[0].startswith("--"):
        raise Fail("폴더 이름을 적어 주세요. 예: python ig_publish.py reel 툴만화_001_WisprFlow")
    publish_reel(env, pick_post(find_posts(env), args[0]), "--go" in args)


def cmd_publish(env, args):
    if not args or args[0].startswith("--"):
        raise Fail("폴더 이름을 적어 주세요. 예: python ig_publish.py publish 2026-11-24_화_커버스토리_01_2026AI지도")
    publish_one(env, pick_post(find_posts(env), args[0]), "--go" in args)


def cmd_run(env, args):
    go = "--go" in args
    if go:
        maybe_refresh(env)
    posts = find_posts(env)
    grace = timedelta(hours=float(env.get("GRACE_HOURS", "3")))
    now = now_kst()
    due = []
    for r in load_schedule():
        if r.get("approved", "").strip().upper() != "Y":
            continue
        try:
            at = datetime.strptime(f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except ValueError:
            print(f"날짜/시각 형식 오류: {r}")
            continue
        if at <= now <= at + grace:  # 놓친 편은 몰아 올리지 않음 (publish 로 직접)
            due.append(r)
    if not due:
        print(f"{now:%m/%d %H:%M} 지금 올릴 승인된 게시물 없음.")
        return
    for r in due:
        p = posts.get(r["folder"])
        kind = row_type(r)
        if not p and kind != "reel":
            print(f"[멈춤] {r['folder']}: 폴더/zip 을 못 찾음")
            continue
        try:
            if kind == "reel":
                publish_reel(env, p, go, name=r["folder"])
            else:
                publish_one(env, p, go)
        except Fail as e:
            print(f"  ✗ {r['folder']} 실패: {e}")
    cmd_plan(env, [])


def cmd_refresh(env, args):
    maybe_refresh(env, force=True)


def main(argv):
    cmds = {"check": cmd_check, "plan": cmd_plan, "publish": cmd_publish, "reel": cmd_reel, "run": cmd_run,
            "refresh": cmd_refresh}
    if not argv or argv[0] not in cmds:
        print(__doc__)
        return 1
    try:
        cmds[argv[0]](load_env(), argv[1:])
        return 0
    except Fail as e:
        print("✗ " + str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
